"""构建：把 [build] 表翻译成 shell，在容器里降权执行。"""

from __future__ import annotations

import datetime
import os
import subprocess

from .common import BUILDENV_IMAGE, BUILD_DIR, CCACHE_DIR, build_jobs, die, log

from .spec import expand

# ------------------------------------------------------------ 生成构建脚本

def make_build_script(spec: dict) -> str:
    """把 [build] 表翻译成 shell。steps.sh 可通过重定义函数覆盖。"""
    b = spec.get("build", {})
    kind = b.get("type", "custom")
    oot = bool(b.get("out_of_tree", False))

    if oot and kind != "autotools":
        die(f"{spec['package']['name']}: out_of_tree 目前只支持 autotools"
            f"（meson 和 cmake 本来就是离开源码树构建的）")

    # 脚本自带 cd：它会被降权后由 setpriv 单独执行，不能依赖调用方的 cwd
    header = 'cd "$SRCDIR"\n'
    if oot:
        header += 'BUILDDIR="$SRCDIR/_build"\n'

    # 补丁：源码树就位之后、configure 之前应用。
    # 路径相对配方目录，配方目录通过 $PKGDIR 已送进容器。
    for entry in b.get("patches", []):
        p = expand(spec, entry)
        # --fuzz=0：必须精确匹配。patch 默认容忍 2 行模糊匹配，那样一个已经
        # 不适用的补丁会被「差不多」地贴上去，产出无声的错误代码——等到运行
        # 时才以看不懂的方式炸掉。宁可在构建时响亮地失败。
        header += (f'echo "[补丁] {p}"\n'
                   f'patch -p1 --fuzz=0 --no-backup-if-mismatch -i "$PKGDIR/{p}"\n')

    if kind == "autotools":
        opts = " ".join(b.get("configure", ["--prefix=/usr"]))
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
        opts = " ".join(b.get("configure", ["--prefix=/usr", "--buildtype=release"]))
        body = f"  meson setup _build {opts}\n  meson compile -C _build"
        pkgstep = '  DESTDIR="$PKGDEST" meson install -C _build'
    elif kind == "cmake":
        opts = " ".join(b.get("configure", ["-DCMAKE_INSTALL_PREFIX=/usr"]))
        body = f"  cmake -B _build {opts}\n  cmake --build _build"
        pkgstep = '  DESTDIR="$PKGDEST" cmake --install _build'
    elif kind == "custom":
        body = "  : # 由 steps.sh 提供"
        pkgstep = "  : # 由 steps.sh 提供"
    else:
        die(f"{spec['package']['name']}: 不认识的构建类型 {kind!r}")

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


def run_in_container(srcrel: str, arch: str, spec: dict, epoch: int) -> None:
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

    # ccache 统计。冷缓存时全是 miss，这是正常的——价值在第二次构建。
    ccache_stats = (
        "\n# ccache 统计\n"
        "if command -v ccache >/dev/null 2>&1; then\n"
        "  ccache --print-stats 2>/dev/null | awk -F'\\t' '\n"
        '    $1=="direct_cache_hit"       { d = $2 }\n'
        '    $1=="preprocessed_cache_hit" { p = $2 }\n'
        '    $1=="cache_miss"             { m = $2 }\n'
        "    END { n = d + p + m\n"
        '          if (n > 0) printf "[ccache] 命中 %d / %d（%.0f%%）\\n", d+p, n, (d+p)*100/n }\'\n'
        '  echo "[ccache] 缓存体积 $(du -sh "$CCACHE_DIR" 2>/dev/null | cut -f1)"\n'
        "fi\n"
    )

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
        'if [ -d /in/sysroot ] && [ -n "$(ls -A /in/sysroot 2>/dev/null)" ]; then\n'
        '  echo "[依赖] 合并 $(find /in/sysroot \\( -type f -o -type l \\) | wc -l | tr -d " ") 项到 /"\n'
        "  cp -a /in/sysroot/. /\n"
        "fi\n"
        "\n"
        "# 容器内工作区：复制源码与脚本，属主交给宿主用户\n"
        "#\n"
        "# 路径定成 /build 而不是 /work-local：它是容器自己的文件系统（不是\n"
        "# 绑定挂载，原因见本函数开头的注释），用标准名字便于别人复现，也让\n"
        "# 产物里出现的路径看起来正常。\n"
        "WORK=/build\n"
        'rm -rf "$WORK"\n'
        'mkdir -p "$WORK/dest"\n'
        'cp -a /in/src "$WORK/src"\n'
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
        '  bash "$WORK/run.sh"\n'
        "\n"
        + ccache_stats
        + strip_block
        + "# 产物拷回宿主\n"
        "mkdir -p /in/dest\n"
        'cp -a "$WORK/dest/." /in/dest/\n'
    )

    # MAKEFLAGS 通过环境变量传给所有 make（含子 make），省得每个配方各写一遍 -j。
    jobs = str(build_jobs())
    cmd = [
        "docker", "run", "--rm",
        # 固定主机名。docker 默认给容器一个随机主机名（容器 ID），
        # 于是任何调用 gethostname() 的构建都会嵌入一个随机值——实测两次
        # 分别是 0fdeea78b33b 和 97ec2399f6f9。名字本身不重要（它是构建
        # 容器的，不是成品系统的），重要的是它不变。
        "--hostname", "build",
        "-e", f"HOSTUID={os.getuid()}",
        "-e", f"HOSTGID={os.getgid()}",
        "-e", f"PKGARCH={arch}",
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
        "-v", f"{BUILD_DIR}:/in",
        "-v", f"{CCACHE_DIR}:/ccache",
        f"{BUILDENV_IMAGE}:{arch}",
        "bash", "-c", wrapper,
    ]

    when = datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc)
    log("构建", f"{BUILDENV_IMAGE}:{arch}  (容器内构建, -j{jobs})")
    log("时间戳", f"SOURCE_DATE_EPOCH={epoch}  ({when:%Y-%m-%d %H:%M:%S} UTC)")
    proc = subprocess.run(cmd)
    if proc.returncode != 0:
        die(f"构建失败（退出码 {proc.returncode}）")


