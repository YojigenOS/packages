"""构建：把 [build] 表翻译成 shell，在容器里降权执行。"""

from __future__ import annotations

import datetime
import os
import subprocess

from .common import (BUILDENV_IMAGE, BUILD_DIR, CCACHE_DIR, CCACHE_MAXSIZE,
                     CCACHE_MOUNT, build_jobs, die, log, target_triple)

from .spec import expand

#: 我们的架构名 → docker 的平台名。两套命名不一样，别混。
DOCKER_PLATFORM = {
    "aarch64": "linux/arm64",
    "x86_64": "linux/amd64",
}

# ------------------------------------------------------------ 生成构建脚本

def make_build_script(spec: dict, arch: str = "") -> str:
    """把 [build] 表翻译成 shell。steps.sh 可通过重定义函数覆盖。"""
    b = spec.get("build", {})
    kind = b.get("type", "custom")
    oot = bool(b.get("out_of_tree", False))

    if oot and kind != "autotools":
        die(f"{spec['package']['name']}: out_of_tree 目前只支持 autotools"
            f"（meson 和 cmake 本来就是离开源码树构建的）")

    # 脚本自带 cd：它会被降权后由 setpriv 单独执行，不能依赖调用方的 cwd
    #
    # ★ set -eo pipefail 必须在这里，不能只写在外层 wrapper 上。
    #
    # 这是一个真出过事的坑：外层的 wrapper（bash -c）确实有 set -euo pipefail，
    # 但 run.sh 是通过 `setpriv ... bash "$WORK/run.sh"` 起的【另一个 bash 进程】，
    # 而 set -e 不会被 bash 子进程继承。
    #
    # 于是 run.sh 里没有 set -e，末尾那两行
    #
    #     build
    #     package
    #
    # 就变成了 sequential 而不是 short-circuit：build 失败了 package 照跑，
    # run.sh 的退出码是 package 的，而 package 往往能"成功"（哪怕只装进去
    # 一个文件）。外层看到 0，驱动打印「完成」。
    #
    # 实测就是内核：make 报 Error 2、两条 install 都 cannot stat，然后驱动
    # 说「[完成] linux-7.2.9-1（2 项）」——产物里只有一个 321 KB 的 config，
    # 而那 321 KB 还会被当成依赖合并进后面的构建。
    #
    # 不带 -u：steps.sh 是别人写的脚本，引用一个没定义的变量应该只是空字符串，
    # 不该让构建崩掉。外层用 -u 是因为外层是我们自己写的。
    header = 'set -eo pipefail\ncd "$SRCDIR"\n'
    if oot:
        header += 'BUILDDIR="$SRCDIR/_build"\n'

    # autoreconf：从 git 树构建 autotools 项目时必须的一步。
    #
    # 【为什么需要它】发行版 tarball 里带着上游跑好的 configure，git 树里
    # 没有——那些生成物不在版本控制里。项目决定 GitHub 类的源改用 commit
    # （release 附件可以被替换和移动，commit 不会），于是这一步成了必需。
    #
    # 放在补丁之后、configure 之前：有些补丁就是改 configure.ac 的。
    if b.get("autoreconf"):
        # ★ 先查 autopoint。
        #
        #   autoreconf 需要 autopoint 时会【静默跳过】它——找不到也不报错——
        #   然后 automake 才失败，报的是缺 build-aux/config.rpath。那句话
        #   完全指不到真正的原因（实测为此查了一阵）。
        #
        #   而 Debian 的 autopoint 是独立的包：装了 gettext 也没有它。
        #
        #   所以这里把静默变成响亮：项目要 gettext 而环境里没有 autopoint 时，
        #   当场说清楚缺什么、为什么、怎么补。
        header += (
            'if grep -qs "AM_GNU_GETTEXT" configure.ac \\\n'
            '   && ! command -v autopoint >/dev/null 2>&1; then\n'
            '  echo "[autoreconf] configure.ac 用了 AM_GNU_GETTEXT，但环境里没有 autopoint。" >&2\n'
            '  echo "  autoreconf 会静默跳过那一步，然后 automake 报缺 build-aux/config.rpath——" >&2\n'
            '  echo "  那个报错指不到这里。装上 autopoint（Debian 里它是独立的包，gettext 不含它）。" >&2\n'
            '  exit 1\n'
            'fi\n'
            'echo "[autoreconf] 生成 configure 和 Makefile.in"\n'
            'autoreconf -fi\n')


    # 补丁：源码树就位之后、configure 之前应用。
    # 路径相对配方目录，配方目录通过 $PKGDIR 已送进容器。
    for entry in b.get("patches", []):
        p = expand(spec, entry, arch)
        # --fuzz=0：必须精确匹配。patch 默认容忍 2 行模糊匹配，那样一个已经
        # 不适用的补丁会被「差不多」地贴上去，产出无声的错误代码——等到运行
        # 时才以看不懂的方式炸掉。宁可在构建时响亮地失败。
        header += (f'echo "[补丁] {p}"\n'
                   f'patch -p1 --fuzz=0 --no-backup-if-mismatch -i "$PKGDIR/{p}"\n')

    # 库目录必须显式钉死成 /usr/lib，三个构建系统都是。
    #
    # 按决策记录 0001，我们做 usr-merge：/usr/lib64 是指向 lib 的符号链接。
    # 而 autoconf 有个启发式——64 位系统上如果编译器的默认搜索路径里有
    # lib64，它就把 libdir 定成 ${prefix}/lib64。于是包会装出一个【真目录】
    # /usr/lib64，拼 rootfs 时和那个符号链接撞车：
    #
    #     cp: cannot overwrite non-directory '/rootfs/./usr/lib64'
    #         with directory '.../libffi-3.8.0-1/payload/./usr/lib64'
    #
    # 实测中招的是 libffi（4 个文件）。但这是一类问题，不是它一个包的问题，
    # 所以在默认值里钉死，而不是每个配方各写一遍——写一遍就会有人忘。
    #
    # cmake 的 GNUInstallDirs 和 meson 也有各自的 multiarch 习惯，一并钉住。
    LIBDIR = "--libdir=/usr/lib"

    if kind == "autotools":
        opts_list = list(b.get("configure", ["--prefix=/usr", LIBDIR]))
        # ★ --host 无条件加，写在这里而不是每个配方里。
        #
        #   37 个 autotools 配方各写一遍就等于写 37 遍，而写 37 遍就会有人忘。
        #   这和上面 LIBDIR 的理由是同一个（见决策记录 0001 的那段说明）。
        #
        #   不传 --host 时 configure 会跑 config.guess，而它不认识 Yojigen OS，
        #   于是 bash --version 显示 aarch64-unknown-linux-gnu。
        #
        #   ★★ 必须【连 --build 一起传】，而且传同一个三元组。
        #
        #   只传 --host 的话，configure 拿 config.guess 得到的 build 和它一比，
        #   发现不一样，就认为【在交叉编译】——于是去找带前缀的工具：
        #
        #       aarch64-yojigen-linux-gnu-ar: command not found
        #       configure: error: could not determine aarch64-yojigen-linux-gnu-ar interface
        #
        #   实测（binutils 2.47，Debian 环境）：
        #
        #       --host=X 单独          →  AR = X-ar          ✗ 那个工具不存在
        #       --build=X --host=X     →  AR = ar            ✓
        #
        #   build == host 时 configure 走的是【本地构建】那条路，用不带前缀的
        #   工具名，同时把产物记录成我们的三元组——这正是我们要的：换个名字的
        #   本地构建，不是交叉编译。
        #
        #   ★ 留了覆盖的口子：配方自己写了 --host= 就不加。交叉编译之类的
        #     场合需要它。
        if not any(o.startswith("--host=") for o in opts_list):
            triple = target_triple(arch)
            opts_list.append(f"--build={triple}")
            opts_list.append(f"--host={triple}")
        opts = " ".join(opts_list)
        if oot:
            body = ('  mkdir -p "$BUILDDIR"\n'
                    '  cd "$BUILDDIR"\n'
                    f'  "$SRCDIR/configure" {opts}\n'
                    '  make')
            pkgstep = ('  cd "$BUILDDIR"\n'
                       '  make DESTDIR="$PKGDEST" install')
        else:
            body = f"  ./configure {opts}\n  make"
            pkgstep = '  make DESTDIR="$PKGDEST" install'
    elif kind == "meson":
        opts = " ".join(b.get("configure",
                              ["--prefix=/usr", "--libdir=lib", "--buildtype=release"]))
        body = f"  meson setup _build {opts}\n  meson compile -C _build"
        pkgstep = '  DESTDIR="$PKGDEST" meson install -C _build'
    elif kind == "cmake":
        opts = " ".join(b.get("configure",
                              ["-DCMAKE_INSTALL_PREFIX=/usr",
                               "-DCMAKE_INSTALL_LIBDIR=lib"]))
        body = f"  cmake -B _build {opts}\n  cmake --build _build"
        pkgstep = '  DESTDIR="$PKGDEST" cmake --install _build'
    elif kind == "custom":
        body = "  : # 由 steps.sh 提供"
        pkgstep = "  : # 由 steps.sh 提供"
    else:
        die(f"{spec['package']['name']}: 不认识的构建类型 {kind!r}")

    # ★★ 检查 [build] 里有没有【这个类型不认识的键】。
    #
    #   为什么要有这一步：驱动读键用的是 `b.get("configure", 默认值)`——
    #   **键名写错了不报错，它静静地用默认值**。
    #
    #   实测撞到：给 fastfetch 写了 13 个 CMake 选项，键名写成 `options`
    #   （正确的是 `configure`）。包**建成功了**，用默认值编的——少了
    #   -DENABLE_ZLIB、-DENABLE_WCWIDTH 这些，而那些正是"中文对齐"要的。
    #   界面是绿的，产物是错的。
    #
    #   这和"CMake 对未知的 -D 变量不报错"是同一类事，只不过这次元凶是
    #   TOML 的键名。**一个被静默忽略的字段等于没写**。
    # 这个集合是【从驱动代码里数出来的】——grep 一遍所有 b.get(...)：
    #     type · out_of_tree · autoreconf · patches · configure · nostrip · cflags
    # 前三类（build_type / cflags / patches）对所有 kind 都生效。
    known = {
        "type", "out_of_tree", "autoreconf", "patches", "configure", "nostrip", "cflags",
    }
    unknown = sorted(set(b) - known)
    if unknown:
        die(f"{spec['package']['name']}: [build] 里有多余的键 {unknown}"
            f"（{kind} 只认 {sorted(known)}）—— 写错了不会报错，只会被静默忽略")

    return (
        header
        + "\n"
        "build() {\n" + body + "\n}\n"
        "\n"
        "package() {\n" + pkgstep + "\n}\n"
        "\n"
        "# steps.sh 若存在，可以通过重定义上面任意函数来覆盖默认行为\n"
        'if [ -f "$WORK/steps.sh" ]; then . "$WORK/steps.sh"; fi\n'
        "\n"
        "build\n"
        "package\n"
    )


def run_in_container(srcrel: str, srcname: str, arch: str, spec: dict, epoch: int,
                     unpack: bool = True) -> None:
    pkg = spec["package"]
    (BUILD_DIR / "sysroot").mkdir(parents=True, exist_ok=True)
    CCACHE_DIR.mkdir(parents=True, exist_ok=True)

    # 整个构建过程发生在容器内的 Linux 原生文件系统里，只把源码送进去、
    # 把产物取出来。两个原因：
    #
    #   1. macOS 的 APFS 默认大小写不敏感。glibc 的构建会产生 stamp.oS 和
    #      stamp.oST 这类只差大小写的文件，在绑定挂载上会互相覆盖，报
    #      "are the same file"。见 docs/决策记录/0002。
    #   2. 绑定挂载上的海量小文件 I/O 比容器内慢一个数量级——而构建过程
    #      恰好就是海量小文件读写。
    #
    # 权限：容器以 root 启动，因为要把依赖合并进根文件系统；合并完成后
    # 立刻降权到宿主用户再执行构建。以 root 构建会踩两部分上游检查
    # （GNU 的 "you should not run configure as root"），也会让产物属主不对。
    # strip：去掉调试信息。不 strip 的话 gcc 一个包就占 2 GB——
    # 实测 cc1 407 MB → 36 MB（90%），libstdc++.a 51 MB → 6 MB（87%）。
    #
    #   .so / 可执行文件 → --strip-unneeded
    #   .a              → --strip-debug（用 --strip-unneeded 会破坏静态库）
    #   .o              → 绝对不碰：链接时需要它们的目标码与重定位信息
    #
    # 非 ELF 文件（链接脚本、shell 脚本等）会被 strip 拒绝，不影响构建。
    # 个别需要保留符号的包可以声明 [build] nostrip = true 退出。
    b = spec.get("build", {})
    if b.get("nostrip"):
        strip_block = ""
    else:
        strip_block = (
            "\n# strip：去掉调试信息\n"
            "#\n"
            "# 计量用【文件真实字节数】而不是 du：du 数的是磁盘分配块，会随文件\n"
            "# 系统和分配策略变化。glibc 那次报了「57256 KB → 68208 KB（省 -19%）」，\n"
            "# 看着像 strip 把包撑大了 11 MB——实际上那棵树一直是 68 MB，是 du 在\n"
            "# 容器文件系统上给出的两个数字对不上。对一个讲究可复现的构建来说，\n"
            "# 用一个依赖文件系统的指标本来就不合适。\n"
            "size_kb() { find \"$1\" -type f -printf '%s\\n' 2>/dev/null "
            "| awk '{ s += $1 } END { printf \"%d\", s / 1024 }'; }\n"
            'before=$(size_kb "$WORK/dest")\n'
            'find "$WORK/dest" -type f \\( -name "*.so" -o -name "*.so.*" -o -perm -u+x \\) \\\n'
            '     ! -name "*.a" ! -name "*.o" ! -name "*.la" -print0 2>/dev/null \\\n'
            '  | xargs -0 -r strip --strip-unneeded 2>/dev/null || true\n'
            'find "$WORK/dest" -type f -name "*.a" -print0 2>/dev/null \\\n'
            '  | xargs -0 -r strip --strip-debug 2>/dev/null || true\n'
            'after=$(size_kb "$WORK/dest")\n'
            'if [ "$before" -gt 0 ]; then pct=$(( (before-after)*100/before )); else pct=0; fi\n'
            'echo "[strip] ${before} KB → ${after} KB（省 ${pct}%）"\n'
        )

    # ccache 统计。
    #
    # 【只报能验证的东西。】原来这里读 `ccache --print-stats` 的命中率，但
    # 实测它不可靠：ccache 4.14 把统计分片存在每个缓存子目录的 `stats` 文件里
    # （0/0/stats … f/f/stats），而我们的构建确实在写它们——构建前后分片 md5
    # 变了、一分钟内 215 个分片被动过。可 `--print-stats` 汇总出来的数字纹丝
    # 不动，连着四次构建一模一样。
    #
    # 一行会骗人的日志比没有日志更糟：它让人以为缓存没生效，然后去查一个不
    # 存在的问题（我就查了半天）。所以换成两个直接可数的量。
    ccache_stats = (
        "\n# ccache 状态\n"
        "if [ -d /usr/lib/ccache ]; then\n"
        '  echo "[ccache] 缓存体积 $(du -sh "$CCACHE_DIR" 2>/dev/null | cut -f1'
        ' || echo 未知)"\n'
        '  echo "[ccache] 缓存条目 $(find "$CCACHE_DIR" -name stats 2>/dev/null'
        ' | wc -l | tr -d " ")"\n'
        "fi\n"
    )

    # 配方可以追加 CFLAGS。用于「这个包的代码比当前语言标准老」这类情况。
    #
    # 具体起因：GCC 15 起默认 C 标准是 C23，而 C23 把 `()` 的含义从
    # 「参数未知」改成了「零参数」。gmp 6.3.0 的 configure 里有一段 2003 年
    # 写的 K&R 测试代码，于是它判定「没有可用的编译器」：
    #
    #     conftest.c:12:48: error: too many arguments to function 'g'
    #         void g(){}          ← C23 里这是「零参数」
    #         g(i,d[i].src,...);  ← 于是传参数成了错误
    #
    # 这是语言标准的破坏性变更，不是 bug。项目选择了保持 gnu23，遇到不兼容
    # 的包就【按包声明】回退标准，而不是全局降级——new 代码该用新标准。
    #
    # 放在配方里而不是 steps.sh 里，是为了让这件事在配方上看得见：翻到
    # spec.toml 就知道这个包为什么要用旧标准。
    extra_cflags = " ".join(b.get("cflags", []))
    cflags_line = (f'export CFLAGS="$CFLAGS {extra_cflags}"\n'
                   f'export CXXFLAGS="$CFLAGS"\n') if extra_cflags else ""

    wrapper = (
        "set -euo pipefail\n"
        "\n"
        "# 固定的 umask。不同机器的 umask 不同，会让产物里的文件权限跟着变——\n"
        "# 而权限是产物的一部分。\n"
        "umask 022\n"
        "\n"
        "# 加固与调试信息策略。集中写在这里，而不是每个配方各写一遍。\n"
        "#\n"
        "#   -g0                       不生成调试信息。我们最后一律 strip，\n"
        "#                             留着它只是白付编译时间和磁盘——而 gcc\n"
        "#                             那一步是构建的大头，这里省得最多。\n"
        "#   -fstack-protector-strong  栈保护。\n"
        "#   -U_FORTIFY_SOURCE         先撤销再定义：少数包会自己定义它，\n"
        "#   -D_FORTIFY_SOURCE=3       不撤销就会重复定义、报警告。\n"
        "#   -mbranch-protection=standard\n"
        "#                             arm64 的 BTI + PAC。指令编码在 HINT\n"
        "#                             空间里，老 CPU 当 NOP 执行——不牺牲\n"
        "#                             兼容性。x86_64 的对应物是 CET。\n"
        "#   LDFLAGS 的 relro/now       完整 RELRO。实测我们原本只有部分\n"
        "#                             （有 GNU_RELRO 但没有 BIND_NOW）。\n"
        "#\n"
        "# 不用 -ffile-prefix-map：容器内的路径本来就是固定的（/build/...），\n"
        "# 别人用同一个驱动构建也会得到同样的路径。可复现构建的标准做法是\n"
        "# 「让路径可预测」，我们已经做到了——映射是路径不可控时的补救手段。\n"
        'case "$PKGARCH" in\n'
        '  aarch64) ARCH_HARDEN="-mbranch-protection=standard" ;;\n'
        '  x86_64)  ARCH_HARDEN="-fcf-protection=full" ;;\n'
        '  *)       ARCH_HARDEN="" ;;\n'
        "esac\n"
        'export CFLAGS="-g0 -O2 -fstack-protector-strong -U_FORTIFY_SOURCE -D_FORTIFY_SOURCE=3 $ARCH_HARDEN"\n'
        'export CXXFLAGS="$CFLAGS"\n'
        'export LDFLAGS="-Wl,-z,relro -Wl,-z,now"\n'
        + cflags_line +
        "\n"
        "# ccache 的代理符号链接目录，放进 PATH 最前面。\n"
        "#\n"
        "# 显式设置，不靠镜像的 ENV：构建环境是可换的（SKIRT_BUILDENV），而\n"
        "# docker import 出来的镜像【不带 ENV】——靠镜像设 PATH 会让「这次构建\n"
        "# 到底用没用缓存」变成一件看不见的事。这正是 0011 说的那类隐式依赖。\n"
        "#\n"
        "# 目录不存在就跳过（ccache 没装，或者装在别处）。\n"
        "if [ -d /usr/lib/ccache ]; then\n"
        '  export PATH="/usr/lib/ccache:$PATH"\n'
        "fi\n"
        "\n"
        'cleanup() { chown -R "$HOSTUID:$HOSTGID" /in 2>/dev/null || true; }\n'
        "trap cleanup EXIT\n"
        "\n"
        "# 依赖包的 payload 合并进根文件系统（需要 root）\n"
        "#\n"
        "# 【为什么不用 cp -a /in/sysroot/. /】那会覆盖构建容器自己的布局，\n"
        "# 而容器里有些路径是【它的命根子】：\n"
        "#\n"
        "#   x86_64: /lib64 -> usr/lib64，而 usr/lib64/ld-linux-x86-64.so.2\n"
        "#           是动态链接器。我们的 filesystem 和 gcc 都提供\n"
        "#           usr/lib64 -> lib，Links 一改，链接器就悬空了——\n"
        "#           **容器里所有动态链接的程序立刻全废**。\n"
        "#\n"
        "# 实测的表现：glibc 构建 11 秒就死，日志里只有「合并 N 项到 /」和\n"
        "# 一句失败，完全看不出原因。查了很久。\n"
        "#\n"
        "#   aarch64 一直没暴露：链接器在 /lib/ld-linux-aarch64.so.1，\n"
        "#   而 /lib -> usr/lib 两边一致。只有 x86_64 的 lib64 家族有分歧。\n"
        "#\n"
        "# 【为什么排除是对的】这里是「合并进构建容器」，不是「拼装我们的根」。\n"
        "# filesystem 描述的是【我们的根】，而构建容器的布局是 Debian 的。\n"
        "# 包的内容照样进 usr/，受影响的只有这几个位置。\n"
        "#\n"
        "# 用 tar --exclude 而不是 cp：排除项里有的是【子路径】\n"
        "# （usr/lib64），顶层循环盖不住；而且 tar 处理符号链接的方式正是\n"
        "# 我们要的（cp -a 会把目标已有的链接替换掉）。\n"
        'if [ -d /in/sysroot ] && [ -n "$(ls -A /in/sysroot 2>/dev/null)" ]; then\n'
        '  echo "[依赖] 合并 $(find /in/sysroot \\( -type f -o -type l \\) | wc -l | tr -d " ") 项到 /（跳过容器自己的布局）"\n'
        '  # 排除容器的挂载点——往那里写没意义，而且 tar 会因为只读或\n'
        '  # 「File exists」（Docker 把 /etc/hostname、/etc/hosts 之类绑定挂载进来）\n'
        '  # 而失败。**动态算，不写死清单**：写死的清单在别的运行时上就不对了。\n'
        '  EXCLUDES=""\n'
        '  while read -r _dev mnt _rest; do\n'
        '    [ "$mnt" = "/" ] && continue\n'
        '    EXCLUDES="$EXCLUDES --exclude=.$mnt"\n'
        '  done < /proc/mounts\n'
        '  # 加上我们自己定的布局（见上面的注释）\n'
        '  EXCLUDES="$EXCLUDES --exclude=./bin --exclude=./sbin --exclude=./lib"\n'
        '  EXCLUDES="$EXCLUDES --exclude=./lib64 --exclude=./usr/lib64"\n'
        '  tar -C /in/sysroot -cf - $EXCLUDES . | tar -C / -xf -\n'
        "fi\n"
        "\n"
        "# 容器内工作区：复制源码与脚本，属主交给宿主用户\n"
        "#\n"
        "# 路径定成 /build 而不是 /work-local：它是容器自己的文件系统（不是\n"
        "# 绑定挂载，原因见本函数开头的注释），用标准名字便于别人复现，也让\n"
        "# 产物里出现的路径看起来正常。\n"
        "WORK=/build\n"
        'rm -rf "$WORK"\n'
        'mkdir -p "$WORK/dest" "$WORK/src"\n'
        # ★ 源码在这里解压，不在宿主上。
        #
        # 宿主是 macOS，APFS 默认大小写不敏感。内核源码里有 13 组只差大小写的
        # 文件（xt_MARK.h / xt_mark.h、xt_TCPMSS.c / xt_tcpmss.c …），在宿主上
        # 解开就会互相覆盖——实测 13 组全变成 0 组，然后构建在 47 分钟后才失败，
        # 报的是「找不到 xt_mark.h」这种指不到根因的错误。
        #
        # 容器里是 Linux 原生文件系统，大小写敏感，两个文件能共存。
        # 宿主只读 tar 里的名字算源码根目录叫什么（见 source.py 的 srcrel_of），
        # 那是字符串操作，不涉及文件系统语义。
        #
        # 这也正是决策记录 0002 的意图：「整个构建过程发生在容器内的 Linux 原生
        # 文件系统里，只把源码送进去、把产物取出来」。源码以前是在宿主上就解开
        # 了，等于「送进去」之前已经被破坏过一次。
        # ★ 源码包可能是 tar，也可能是 zip（Python 的 wheel 就是 zip）。
        #
        #   容器里两个环境（Debian 的 buildenv、我们自己的 devel）都有 python3，
        #   所以用它的 zipfile 模块解——不额外依赖 unzip。
        #
        #   tarfile 认不出 zip：实测它把 gz/bz2/xz/zst/tar 五种方法都试过，
        #   最后报 "file could not be opened successfully"。
        #
        # ★ 写成单行 if/else。第一版用了多行 + 行尾反斜杠续行，结果
        #   `python3 -m zipfile … \` 后面接 `else` 会把两行接成一行，
        #   变成 `python3 … else tar …` —— 语法错误。续行符只在【确实要
        #   续行】时用，命令之间的分界靠 `;`。
        # ★ unpack = false 时【不解压】——那个 source 本身就是源码，
        #   直接放进 $SRCDIR 下（用它的原文件名）。见 commands.py 里的说明。
        f'[ -f /in/src.tar ] && {{ '
        f'if [ "{("1" if unpack else "0")}" = "0" ]; then cp /in/src.tar "$WORK/src/{srcname}"; '
        'elif python3 -c "import zipfile,sys; sys.exit(0 if zipfile.is_zipfile(\'/in/src.tar\') else 1)" 2>/dev/null; '
        'then python3 -m zipfile -e /in/src.tar "$WORK/src"; '
        # ★ .tar.bz2 靠 GNU tar 自己去 exec bzip2。所以【构建依赖里要有 bzip2】
        #   ——没有它，tar 报 `cannot exec bzip2`。
        #
        #   试过用 python3 的 tarfile 代替（想省掉这个依赖）：不行。python3 的
        #   bz2 不是内建的，它是 _bz2 这个 C 模块，要链 libbz2——而我们连
        #   bzip2 都没有，所以解释器里根本没有这个模块：
        #       tarfile.CompressionError: bz2 module is not available
        #   （zlib 那条路能走通，是因为我们【有】zlib。）
        'else tar -xf /in/src.tar -C "$WORK/src"; fi; }\n'
        '[ -f /in/steps.sh ] && cp /in/steps.sh "$WORK/steps.sh"\n'
        '[ -d /in/pkg ] && cp -a /in/pkg "$WORK/pkg"\n'
        'cp /in/run.sh "$WORK/run.sh"\n'
        'chown -R "$HOSTUID:$HOSTGID" "$WORK"\n'
        "export WORK\n"
        'export PKGDIR="$WORK/pkg"\n'
        "\n"
        "# 降权执行构建\n"
        'setpriv --reuid="$HOSTUID" --regid="$HOSTGID" --clear-groups \\\n'
        '  env HOME="$WORK" \\\n'
        f'      PKGDEST="$WORK/dest" SRCDIR="$WORK/src/{srcrel}" \\\n'
        # ★ SRCARCHIVE：原始归档的路径。
        #
        # 为什么需要它：不是所有源码都适合「解开再用」。Python 的 wheel 就是
        # 例子——它的安装规则（.data/ 按 sysconfig 铺开、从 entry_points 生成
        # 启动脚本、写 RECORD）是 pip 的职责，而 pip 要的是【那个 .whl 文件】，
        # 不是解开的一棵树。
        #
        # 名字里的 src.tar 是历史遗留：它现在可能是 tar，也可能是 zip（wheel）。
        # 判断方式在下面那段解压逻辑里，用 zipfile.is_zipfile 读魔数。
        f'      SRCARCHIVE="/in/{srcname}" \\\n'
        '  bash "$WORK/run.sh"\n'
        "\n"
        + ccache_stats
        + strip_block
        # ── 删掉生成物性质的路径 ──────────────────────────────────────────
        #
        # usr/share/info/dir 是 install-info 生成的【目录索引】，不是任何包该
        # 提供的东西。每个装了 info 文档的包都会重新生成一份，内容各不相同——
        # 实测 29 个包的 payload 里都有它。而用 apk 装的时候它们互相冲突：
        #
        #     ERROR: hello-2.12.3-r1: trying to overwrite usr/share/info/dir
        #            owned by glibc-2.44-r1.
        #
        # 直接叠 payload 时看不出来（后铺的覆盖先铺的），用包管理器装才暴露。
        # Debian 和 Alpine 也把它当生成物、不进包。
        #
        # ★ 所以放在【驱动层】而不是某个配方里：这是一类路径的通例，不是 hello
        #   或 glibc 的特例。写在配方里就得写 29 遍，而写 29 遍就会有人忘。
        + "\n# 删掉生成物性质的路径（见 build.py 里的说明）\n"
        + 'rm -f "$WORK/dest/usr/share/info/dir"\n'
        + "# 产物拷回宿主\n"
        "mkdir -p /in/dest\n"
        # 拷出来之前先数一遍。
        #
        # 宿主机如果是大小写不敏感的文件系统（macOS 的 APFS 默认就是），
        # 只差大小写的路径【根本不可能共存】——比如 perl 同时装 Pod/（模块）
        # 和 pod/（文档）。cp 会在那里报一句 "File exists"，或者更糟：静默
        # 地少拷。两种都让人查不到真正的原因。
        #
        # 这正是 0002 说的那件事，只是它发生在【拷出来】这一步而不是构建
        # 里：容器内的构建是好的，丢在边界上。数一遍就能把话说清楚。
        'before=$(find "$WORK/dest" \\( -type f -o -type l \\) | wc -l | tr -d " ")\n'
        'cp -a "$WORK/dest/." /in/dest/ || true\n'
        'after=$(find /in/dest \\( -type f -o -type l \\) | wc -l | tr -d " ")\n'
        'if [ "$before" != "$after" ]; then\n'
        '  echo "[产物] ❌ 容器内 $before 项，拷到宿主只剩 $after 项"\n'
        '  echo "[产物] 只差大小写的路径，在大小写不敏感的宿主上无法共存。"\n'
        '  echo "[产物] 下面就是撞车的那几组（容器内的路径）："\n'
        '  find "$WORK/dest" -mindepth 1 | sed "s|^$WORK/dest||" \\\n'
        '    | awk \'{print tolower($0) "\\t" $0}\' | sort \\\n'
        '    | awk -F"\\t" \'{ if ($1 == p && $2 != pp) { print "         " pp "   ↔   " $2 } p=$1; pp=$2 }\' | head -10\n'
        '  echo "[产物] 处理办法：配方不要装这类路径。参考 recipes/perl 的 package()。"\n'
        "  exit 1\n"
        "fi\n"
    )

    # MAKEFLAGS 通过环境变量传给所有 make（含子 make），省得每个配方各写一遍 -j。
    jobs = str(build_jobs())
    cmd = [
        "docker", "run", "--rm",
        # 指定平台。**这不是交叉编译**——是让 docker 跑一个该架构的构建
        # 环境镜像，工具链是那个镜像里的。原生 runner 上是空操作；
        # 别的架构上（本机测 x86_64 走 Rosetta）它负责选对镜像。
        "--platform", DOCKER_PLATFORM.get(arch, f"linux/{arch}"),
        # 固定主机名。docker 默认给容器一个随机主机名（容器 ID），
        # 于是任何调用 gethostname() 的构建都会嵌入一个随机值——实测两次
        # 分别是 0fdeea78b33b 和 97ec2399f6f9。名字本身不重要（它是构建
        # 容器的，不是成品系统的），重要的是它不变。
        "--hostname", "build",
        "-e", f"HOSTUID={os.getuid()}",
        "-e", f"HOSTGID={os.getgid()}",
        "-e", f"PKGARCH={arch}",
        # ★ 目标三元组也注入。规则的定义在 common.target_triple，
        #   这里只是把它送进容器——steps.sh 不该自己拼字符串，那会把规则
        #   散到第二个地方。gcc 的 steps.sh 需要它（它是 custom 类型，
        #   驱动层加不了 --host）。
        "-e", f"PKGTRIPLE={target_triple(arch)}",
        "-e", f"PKGNAME={pkg['name']}",
        "-e", f"PKGVER={pkg['version']}",
        "-e", f"MAKEFLAGS=-j{jobs}",
        # 把「构建发生在什么时候」从产物里抹掉，换成「这份源码是什么时候的」。
        # 没有它，同样的源码每次编译都产出不同的字节——可复现构建无从谈起，
        # 而且这种差异会淹没真正的差异。见 docs/决策记录/0005。
        "-e", f"SOURCE_DATE_EPOCH={epoch}",
        # 区域和时区同样是不可复现的来源：某个工具按本地时区格式化日期，
        # 输出就会随「在哪台机器上构建」而变。
        # 用 C.UTF-8 而不是 C——保持确定性，同时不牺牲 UTF-8 支持
        #（有些包的自检会因为非 ASCII 字符而失败）。
        "-e", "LC_ALL=C.UTF-8",
        "-e", "LANG=C.UTF-8",
        "-e", "TZ=UTC",
        # ★★ 让我们自己的库赢过构建环境镜像里那份。
        #
        #   引导时的构建环境是借来的 Debian，它的库在 /usr/lib/<三元组>/ 下，
        #   而我们的装在 /usr/lib/。问题是【两个目录都在，谁先被找到】：
        #
        #     Debian 的 /etc/ld.so.conf 里根本没有 /usr/lib，只有
        #       /lib/<三元组> 和 /usr/lib/<三元组>
        #     → ldconfig 生成的缓存说 libssl.so.3 => /lib/<三元组>/libssl.so.3
        #     → 我们铺进去的 /usr/lib/libssl.so.3 谁也指不到
        #
        #   实测的后果（openssh 的 configure 当场抓住）：
        #
        #     checking OpenSSL header version...  30500090 (3.5.9)  ← 我们的头
        #     checking OpenSSL library version... 30500070 (3.5.7)  ← 别人的库
        #     configure: error: Your OpenSSL headers do not match your library.
        #
        #   连我们自己的 openssl 命令都是「我们的二进制 + 别人的库」：
        #
        #     $ openssl version
        #     OpenSSL 3.5.9 (Library: OpenSSL 3.5.7)
        #     $ LD_LIBRARY_PATH=/usr/lib openssl version
        #     OpenSSL 3.5.9 (Library: OpenSSL 3.5.9)          ← ✓
        #
        #   【为什么不用别的办法】三个都实测过：
        #
        #     LIBRARY_PATH=/usr/lib   没用。链接期本来就用的我们那份——Debian
        #                             只装 libssl.so.3，没装 -lssl 要的
        #                             libssl.so，所以链接器只能找到我们的。
        #                             而二进制里只记 soname 不记路径，链接期
        #                             赢了也不算数。
        #     合并后跑 ldconfig       没用，而且原因很能说明问题：它【确实】
        #                             把我们的收进去了，但排在后面——
        #
        #                               libssl.so.3 => /lib/aarch64-linux-gnu/…
        #                               libssl.so.3 => /lib/libssl.so.3
        #
        #                             缓存里同名条目是【先到先得】的，所以生来
        #                             就赢不了。
        #
        #   语义上这条也正是要说的：**构建时，我们自己建的包优先于借来的
        #   环境**。引导本来就是一场逐步接管，这是它该有的规则。
        #
        #   （顺带：它在 devel 镜像那条路上是无害的——那里 /usr/lib 本来就是
        #     唯一的地方。）
        "-e", "LD_LIBRARY_PATH=/usr/lib",
        "-e", "LIBRARY_PATH=/usr/lib",
        # ccache 的两个设置也显式传，不靠镜像的 ENV——理由同上面那段 PATH：
        # base-devel 是 docker import 出来的，那种镜像不带 ENV。
        #
        # 实测过一次：导入的 base-devel 里 CCACHE_DIR 是空的。后果有两层——
        # 缓存写进容器内临时的 ~/.cache/ccache（白挂载），而且包装脚本末尾
        # 那句 `du -sh "$CCACHE_DIR"` 在 set -u 下会直接把构建搞崩。
        "-e", f"CCACHE_DIR={CCACHE_MOUNT}",
        "-e", f"CCACHE_MAXSIZE={CCACHE_MAXSIZE}",
        "-v", f"{BUILD_DIR}:/in",
        "-v", f"{CCACHE_DIR}:{CCACHE_MOUNT}",
        f"{BUILDENV_IMAGE}:{arch}",
        "bash", "-c", wrapper,
    ]

    when = datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc)
    log("构建", f"{BUILDENV_IMAGE}:{arch}  (容器内构建, -j{jobs})")
    log("时间戳", f"SOURCE_DATE_EPOCH={epoch}  ({when:%Y-%m-%d %H:%M:%S} UTC)")
    proc = subprocess.run(cmd)
    if proc.returncode != 0:
        die(f"构建失败（退出码 {proc.returncode}）")


