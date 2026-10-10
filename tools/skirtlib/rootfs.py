"""根文件系统拼装：把若干包的 payload 合成一个完整系统，打成 tar。

合并与打包都在容器里做，理由同 docs/决策记录/0002：macOS 的 APFS 默认
大小写不敏感，在宿主机上合并会**静默**吞掉只差大小写的文件——产出一个
看起来正常、实际上缺了东西的 rootfs。这种错误没有任何报错，只有等到
运行时才暴露。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from .common import BUILDENV_IMAGE, BUILD_DIR, OUTPUT_DIR, die, log
from .deps import dep_closure, dep_payload
from .spec import load_spec


def resolve_set(packages: list[str], arch: str) -> list[str]:
    """把点名的包展开成完整集合：依赖在前，被依赖者在前。

    两点与「构建一个包」不同：

      - **只跟 runtime 依赖**。rootfs 是最终系统，不该包含编译器、make
        这类只在构建时需要的东西。
      - **不看 host/provides.txt**。那份列表说的是「构建环境提供什么」，
        和「最终系统里应该有什么」是两回事。glibc 在基线的意思是构建时
        不必处理它，不是说成品系统里不需要 C 库。

    同名包只出现一次。顺序很重要——后面的包会覆盖前面同路径的文件，
    所以必须先铺依赖、再铺依赖它的人。
    """
    order: list[str] = []
    seen: set[str] = set()

    for name in packages:
        spec = load_spec(name)
        for dep in dep_closure(spec, arch, set(), kinds=("runtime",)) + [name]:
            if dep not in seen:
                seen.add(dep)
                order.append(dep)
    return order


def assemble(packages: list[str], arch: str, output: Path | None = None) -> tuple[Path, list[str]]:
    """拼装并打包，返回 (tar 路径, 参与拼装的包列表)。"""
    order = resolve_set(packages, arch)

    rels: list[str] = []
    for name in order:
        _payload, rel = dep_payload(name, arch)
        rels.append(rel)

    out = output or (OUTPUT_DIR / arch / f"yojigenos-rootfs-{arch}.tar.gz")
    out.parent.mkdir(parents=True, exist_ok=True)

    script = (
        "set -euo pipefail\n"
        "mkdir -p /rootfs\n"
        # --numeric-owner 保留 root:root；--sort=name 让同样的输入产出同样的 tar
        + "".join(f'cp -a "/pkgs/{rel}/payload/." /rootfs/\n' for rel in rels)
        + "tar -C /rootfs --numeric-owner --sort=name -czf /out/rootfs.tar.gz .\n"
    )

    proc = subprocess.run([
        "docker", "run", "--rm",
        "-v", f"{OUTPUT_DIR / arch}:/pkgs:ro",
        "-v", f"{BUILD_DIR}:/out",
        f"{BUILDENV_IMAGE}:{arch}",
        "bash", "-c", script,
    ])
    if proc.returncode != 0:
        die(f"拼装失败（退出码 {proc.returncode}）")

    staging = BUILD_DIR / "rootfs.tar.gz"
    if not staging.is_file():
        die("拼装结束但没找到 rootfs.tar.gz")
    out.unlink(missing_ok=True)
    shutil.move(str(staging), out)
    return out, order
