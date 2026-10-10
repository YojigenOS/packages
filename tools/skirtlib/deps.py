"""依赖解析：构建环境基线、依赖闭包、payload 定位、sysroot 叠加。"""

from __future__ import annotations

import shutil
from pathlib import Path

from .common import BUILD_DIR, HOST_PROVIDES_FILE, OUTPUT_DIR, PACKAGES_DIR, ROOT, die, log

from .spec import direct_deps, load_spec

# ---------------------------------------------------------------- 依赖解析

def load_host_provides() -> set[str]:
    """读取 host/provides.txt —— 宿主已经提供的包。"""
    if not HOST_PROVIDES_FILE.is_file():
        return set()
    out = set()
    for line in HOST_PROVIDES_FILE.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            out.add(line)
    return out


def dep_closure(spec: dict, arch: str, host_provides: set[str]) -> list[str]:
    """返回按安装顺序排列的依赖包名（依赖在前，不含自身，已剔除宿主提供的）。

    自依赖不算成环，它是自举的常态：构建 make 需要 make，构建 gcc 需要 gcc
    （gcc 的 stage1 → stage2 正是这个模式）。此时用「上一轮构建出的自己」；
    若还没构建过，说明是第一次，交由当前构建环境提供即可。
    """
    order: list[str] = []
    done: set[str] = set()
    stack: list[str] = []
    self_name = spec["package"]["name"]

    def visit(name: str) -> None:
        if name in host_provides or name in done:
            return
        if name in stack:
            die(f"依赖成环: {' → '.join(stack + [name])}")
        child_spec = load_spec(name)
        stack.append(name)
        for child in direct_deps(child_spec, arch):
            if child == name:
                continue        # 自己依赖自己 = 用上一轮构建产物，不是环
            visit(child)
        stack.pop()
        done.add(name)
        order.append(name)

    for name in direct_deps(spec, arch):
        if name == self_name:
            if built_payload(name, arch) is not None:
                order.append(name)      # 有上一轮的自己，就用它
            else:
                log("解析", f"{name}: 首次构建，使用构建环境自带的版本")
            continue
        visit(name)
    return order


def built_payload(name: str, arch: str) -> Path | None:
    """若该包已构建过则返回其 payload，否则返回 None（不报错）。"""
    if not (PACKAGES_DIR / name / "spec.toml").is_file():
        return None
    pkg = load_spec(name)["package"]
    payload = OUTPUT_DIR / arch / f"{pkg['name']}-{pkg['version']}-{pkg['release']}" / "payload"
    return payload if payload.is_dir() else None


def dep_payload(name: str, arch: str) -> tuple[Path, str]:
    """找到依赖包已构建的 payload。"""
    pkg = load_spec(name)["package"]
    rel = f"{pkg['name']}-{pkg['version']}-{pkg['release']}"
    payload = OUTPUT_DIR / arch / rel / "payload"
    if not payload.is_dir():
        die(
            f"依赖 {name} 还没有构建（找不到 {payload.relative_to(ROOT)}/）\n"
            f"       先执行:  tools/skirt build {name}"
        )
    return payload, rel


def build_sysroot(order: list[str], arch: str) -> Path:
    """把依赖按顺序叠加成 sysroot。后面的包会覆盖前面同路径的文件。"""
    sysroot = BUILD_DIR / "sysroot"
    sysroot.mkdir(parents=True, exist_ok=True)
    for name in order:
        payload, rel = dep_payload(name, arch)
        shutil.copytree(payload, sysroot, symlinks=True, dirs_exist_ok=True)
        n = sum(1 for _ in payload.rglob("*"))
        log("依赖", f"{rel}  ({n} 项)")
    return sysroot


