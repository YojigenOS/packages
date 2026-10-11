"""依赖解析：构建环境基线、依赖闭包、payload 定位、sysroot 叠加。"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from .common import BUILD_DIR, HOST_PROVIDES_FILE, OUTPUT_DIR, RECIPES_DIR, ROOT, die, log, rmtree_force

from .spec import direct_deps, latest_date, load_spec, package_rel, recipe_of

# ---------------------------------------------------------------- 依赖解析

def load_host_provides() -> set[str]:
    """读取 bootstrap/provides.txt —— 宿主已经提供的包。"""
    if not HOST_PROVIDES_FILE.is_file():
        return set()
    out = set()
    for line in HOST_PROVIDES_FILE.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            out.add(line)
    return out


def dep_closure(spec: dict, arch: str, host_provides: set[str],
                kinds: tuple[str, ...] = ("runtime", "build")) -> list[str]:
    """返回按安装顺序排列的依赖包名（依赖在前，不含自身，已剔除宿主提供的）。

    自依赖不算成环，它是自举的常态：构建 make 需要 make，构建 gcc 需要 gcc
    （gcc 的 stage1 → stage2 正是这个模式）。此时用「上一轮构建出的自己」；
    若还没构建过，说明是第一次，交由当前构建环境提供即可。

    kinds 决定跟哪些依赖。默认 runtime + build（构建一个包时需要两者）；
    拼运行时的根文件系统时传 kinds=("runtime",)。
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
        # 子包（glibc-dev 之类）没有自己的配方：它的文件是主包的一部分，
        # 单独装没有意义。所以先跟主包，再把子包自己排上——主包的依赖由
        # visit(主包) 一并带进来，这里不用重复解析。
        parent = recipe_of(name)
        if parent is not None and parent != name:
            stack.append(name)
            visit(parent)
            stack.pop()
            done.add(name)
            order.append(name)
            return
        child_spec = load_spec(name)
        stack.append(name)
        for child in direct_deps(child_spec, arch, kinds):
            if child == name:
                continue        # 自己依赖自己 = 用上一轮构建产物，不是环
            visit(child)
        stack.pop()
        done.add(name)
        order.append(name)

    for name in direct_deps(spec, arch, kinds):
        if name == self_name:
            if built_payload(name, arch) is not None:
                order.append(name)      # 有上一轮的自己，就用它
            else:
                log("解析", f"{name}: 首次构建，使用构建环境自带的版本")
            continue
        visit(name)
    return order


def source_date_epoch(spec: dict, arch: str, host_provides: set[str]) -> int:
    """本包的 SOURCE_DATE_EPOCH。

    「为什么吃整个依赖链而不是只看自己」写在 spec.latest_date 的注释里，
    这里只负责把依赖闭包翻译成一份配方列表——环由 dep_closure 处理。

    闭包里可能有子包名（某个配方声明了 build = ["glibc-dev"]），所以要先
    经 recipe_of 落回配方：子包继承母配方的 date，没有自己的时间戳。
    """
    deps = [load_spec(recipe_of(name) or name)
            for name in dep_closure(spec, arch, host_provides)]
    return latest_date(spec, deps)


def _output_dir(name: str, arch: str) -> tuple[Path, str]:
    """把包名解析成它在 var/output/ 里的目录。

    名字可以是普通包，也可以是子包——两者的目录长得一样，都是
    `<名字>-<版本>-<release>`，版本和 release 跟着产出它的配方走。
    """
    recipe = recipe_of(name)
    if recipe is None:
        die(f"不认识的包: {name}")
    pkg = load_spec(recipe)["package"]
    rel = package_rel(name, pkg["version"], pkg["release"])
    return OUTPUT_DIR / arch / rel, rel


def built_payload(name: str, arch: str) -> Path | None:
    """若该包已构建过则返回其 payload，否则返回 None（不报错）。"""
    if recipe_of(name) is None:
        return None
    payload = _output_dir(name, arch)[0] / "payload"
    return payload if payload.is_dir() else None


def dep_payload(name: str, arch: str) -> tuple[Path, str]:
    """找到依赖包已构建的 payload。"""
    out, rel = _output_dir(name, arch)
    payload = out / "payload"
    if not payload.is_dir():
        die(
            f"依赖 {name} 还没有构建（找不到 {payload.relative_to(ROOT)}/）\n"
            f"       先执行:  skirt/skirt build {recipe_of(name)}"
        )
    return payload, rel


def merge_tree(src: Path, dst: Path) -> None:
    """把 src 合并进 dst，同名项覆盖。

    注意这个函数是给**宿主上拼 sysroot** 用的，没有保护名单——保护名单在
    build.py 里，那是「合并进构建容器」这条完全不同的路径（它用 tar 按路径
    排除，见那里的注释）。两条路别混。

    不能用 shutil.copytree：它的 dirs_exist_ok 只对目录生效，遇到
    「目标已有同名符号链接」会直接抛 File exists。而 gcc 和 filesystem
    都提供 /usr/lib64 → lib，正好撞上——这不是罕见情况，任何两个包
    提供同一路径都会触发。
    """
    dst.mkdir(parents=True, exist_ok=True)
    for entry in src.iterdir():
        target = dst / entry.name

        # 目标已存在就先清掉。三种类型的覆盖规则各不相同，而 shutil 和 os
        # 都不会替我们处理：os.symlink 不覆盖任何东西；shutil.copy2 遇到
        # 符号链接会跟着它跑到链接指向的地方去写。
        if target.is_symlink() or (target.exists() and not target.is_dir()):
            target.unlink()
        elif target.is_dir() and (not entry.is_dir() or entry.is_symlink()):
            rmtree_force(target)

        if entry.is_symlink():
            target.symlink_to(os.readlink(entry))
        elif entry.is_dir():
            merge_tree(entry, target)
            # 目录的权限也要带过来。mkdir 用的是默认的 0755，而 /tmp 是
            # 1777、/root 是 0700——丢了这些模式，构建会以莫名其妙的
            # "Permission denied" 失败，而且看不出和权限有关。
            shutil.copystat(entry, target)
        else:
            shutil.copy2(entry, target)


def build_sysroot(order: list[str], arch: str) -> Path:
    """把依赖按顺序叠加成 sysroot。后面的包会覆盖前面同路径的文件。"""
    sysroot = BUILD_DIR / "sysroot"
    sysroot.mkdir(parents=True, exist_ok=True)
    for name in order:
        payload, rel = dep_payload(name, arch)
        merge_tree(payload, sysroot)
        n = sum(1 for _ in payload.rglob("*"))
        log("依赖", f"{rel}  ({n} 项)")

    # 容器里的挂载点不该出现在 sysroot 里。
    #
    # filesystem 包会提供 /proc /sys /dev 这些空目录做占位（成品系统需要
    # 它们），但在构建容器里它们是只读挂载——合并时 cp -a 会试图给只读
    # 挂载点设置时间戳，报 "Read-only file system" 让整个构建失败。
    #
    # 构建需要的是头文件和库，这些挂载点一个都用不上，直接去掉。
    for mount in ("proc", "sys", "dev", "run"):
        p = sysroot / mount
        if p.exists() or p.is_symlink():
            log("依赖", f"跳过 /{mount}（容器里的挂载点）")
            rmtree_force(p)
    return sysroot


