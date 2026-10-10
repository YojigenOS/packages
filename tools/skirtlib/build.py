"""构建：把 [build] 表翻译成 shell，在容器里降权执行。"""

from __future__ import annotations

import os
import subprocess

from .common import BUILDENV_IMAGE, BUILD_DIR, build_jobs, die, log

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


def run_in_container(srcrel: str, arch: str, spec: dict) -> None:
    pkg = spec["package"]
    (BUILD_DIR / "sysroot").mkdir(parents=True, exist_ok=True)

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
            'before=$(du -sk "$WORK/dest" | cut -f1)\n'
            'find "$WORK/dest" -type f \\( -name "*.so" -o -name "*.so.*" -o -perm -u+x \\) \\\n'
            '     ! -name "*.a" ! -name "*.o" ! -name "*.la" -print0 2>/dev/null \\\n'
            '  | xargs -0 -r strip --strip-unneeded 2>/dev/null || true\n'
            'find "$WORK/dest" -type f -name "*.a" -print0 2>/dev/null \\\n'
            '  | xargs -0 -r strip --strip-debug 2>/dev/null || true\n'
            'after=$(du -sk "$WORK/dest" | cut -f1)\n'
            'if [ "$before" -gt 0 ]; then pct=$(( (before-after)*100/before )); else pct=0; fi\n'
            'echo "[strip] ${before} KB → ${after} KB（省 ${pct}%）"\n'
        )

    wrapper = (
        "set -euo pipefail\n"
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
        "WORK=/work-local\n"
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
        + strip_block
        + "# 产物拷回宿主\n"
        "mkdir -p /in/dest\n"
        'cp -a "$WORK/dest/." /in/dest/\n'
    )

    # MAKEFLAGS 通过环境变量传给所有 make（含子 make），省得每个配方各写一遍 -j。
    jobs = str(build_jobs())
    cmd = [
        "docker", "run", "--rm",
        "-e", f"HOSTUID={os.getuid()}",
        "-e", f"HOSTGID={os.getgid()}",
        "-e", f"PKGARCH={arch}",
        "-e", f"PKGNAME={pkg['name']}",
        "-e", f"PKGVER={pkg['version']}",
        "-e", f"MAKEFLAGS=-j{jobs}",
        "-v", f"{BUILD_DIR}:/in",
        f"{BUILDENV_IMAGE}:{arch}",
        "bash", "-c", wrapper,
    ]

    log("构建", f"{BUILDENV_IMAGE}:{arch}  (容器内构建, -j{jobs})")
    proc = subprocess.run(cmd)
    if proc.returncode != 0:
        die(f"构建失败（退出码 {proc.returncode}）")


