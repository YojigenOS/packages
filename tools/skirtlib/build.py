"""构建：把 [build] 表翻译成 shell，在容器里降权执行。"""

from __future__ import annotations

import os
import subprocess

from .common import BUILDENV_IMAGE, BUILD_DIR, die, log

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
        'cp /in/run.sh "$WORK/run.sh"\n'
        'chown -R "$HOSTUID:$HOSTGID" "$WORK"\n'
        "export WORK\n"
        "\n"
        "# 降权执行构建\n"
        'setpriv --reuid="$HOSTUID" --regid="$HOSTGID" --clear-groups \\\n'
        '  env HOME="$WORK" \\\n'
        f'      PKGDEST="$WORK/dest" SRCDIR="$WORK/src/{srcrel}" \\\n'
        '  bash "$WORK/run.sh"\n'
        "\n"
        "# 产物拷回宿主\n"
        "mkdir -p /in/dest\n"
        'cp -a "$WORK/dest/." /in/dest/\n'
    )

    # MAKEFLAGS 通过环境变量传给所有 make（含子 make），省得每个配方各写一遍 -j。
    jobs = str(os.cpu_count() or 4)
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


