"""构建顺序：按依赖拓扑排序，环断在构建环境基线上。

## 为什么不能只做拓扑排序

35 个配方之间有 108 条依赖边，其中有一个**真的环**：

    glibc 的 build 依赖 binutils 和 gcc
    gcc   的 runtime 依赖 glibc
    glibc → binutils → gcc → glibc

所以顺序**不是配方的纯函数**——它取决于构建环境已经提供什么。这正是
`docs/决策记录/0011` 说的那件事：`host/provides.txt` 里的包由构建环境满足，
指向它们的边断在那里，环就开了。

## 一个容易搞错的地方

基线里的包**仍然要构建**，只是**不等待**。这两件事是分开的：

    依赖边    →  断掉（不等待它先建好）
    构建列表  →  保留（自举的目标就是重建它们）

如果把基线包整个从列表里去掉，就变成「因为 Debian 提供了 glibc，所以永远
不重建 glibc」——那和自举的方向正好相反。
"""

from __future__ import annotations

import graphlib

from .common import PACKAGES_DIR
from .deps import load_host_provides
from .spec import direct_deps, load_spec, recipe_of


def _recipe_names() -> list[str]:
    from .spec import all_recipe_names
    return all_recipe_names()


def build_graph(arch: str, host_provides: set[str],
                names: list[str] | None = None) -> tuple[dict[str, set[str]], list[tuple[str, str]]]:
    """依赖图，以及「哪些边因为基线被断掉了」。

    返回 (图, 断掉的边)。图中的键是配方名，值是需要先建好的配方名。
    """
    names = names if names is not None else _recipe_names()
    known = set(names)
    graph: dict[str, set[str]] = {}
    broken: list[tuple[str, str]] = []

    for name in names:
        spec = load_spec(name)
        edges: set[str] = set()
        for dep in direct_deps(spec, arch, kinds=("runtime", "build")):
            # 依赖可能写成子包名（比如 glibc-dev），要回到它的配方
            recipe = recipe_of(dep)
            if recipe not in known or recipe == name:
                continue
            if recipe in host_provides:
                # 由构建环境满足：不等待它。但仍然记下这条边断了，
                # 因为它正是环被打开的地方，值得让人看见。
                broken.append((name, recipe))
                continue
            edges.add(recipe)
        graph[name] = edges

    return graph, broken


def build_order(arch: str = "aarch64",
                targets: list[str] | None = None) -> tuple[list[str], list[tuple[str, str]]]:
    """能实际跑通的构建顺序。

    targets 为 None 时排全部配方。返回 (顺序, 被基线断掉的边)。

    断不开的环会抛 SystemExit——**不排一个跑不通的顺序**，因为那种顺序的
    表现是「构建失败在某个看不懂的地方」，而失败原因和顺序毫无关系。
    """
    from .common import die

    host_provides = load_host_provides()
    names = _recipe_names()
    known = set(names)

    if targets:
        # 只排目标及其依赖闭包
        want: set[str] = set()
        stack = [recipe_of(t) for t in targets]
        while stack:
            cur = stack.pop()
            if cur in want or cur not in known:
                continue
            want.add(cur)
            spec = load_spec(cur)
            for dep in direct_deps(spec, arch, kinds=("runtime", "build")):
                r = recipe_of(dep)
                if r in known and r not in host_provides:
                    stack.append(r)
        names = [n for n in names if n in want]

    graph, broken = build_graph(arch, host_provides, names)

    try:
        order = list(graphlib.TopologicalSorter(graph).static_order())
    except graphlib.CycleError as e:
        cycle = e.args[1] if len(e.args) > 1 else []
        die(
            "依赖成环，排不出构建顺序：\n"
            f"  {' → '.join(cycle)}\n"
            "\n"
            "环必须由构建环境提供一个起点。把环上最底层的那一个加进\n"
            "host/provides.txt（它会由构建环境满足，不再等待先建好），\n"
            "然后在构建环境里真的提供它。\n"
            "\n"
            f"当前的基线（host/provides.txt）: {sorted(host_provides) or '（空）'}"
        )
        raise  # die 不会返回；这里只是让类型检查满意

    return order, broken


def describe(arch: str = "aarch64", targets: list[str] | None = None) -> None:
    """打印构建顺序和环断在哪里。"""
    from .common import log

    host_provides = load_host_provides()
    order, broken = build_order(arch, targets)

    log("顺序", f"{len(order)} 个包 [{arch}]")
    for i, name in enumerate(order, 1):
        print(f"    {i:3d}  {name}")

    if broken:
        print()
        # 同一个依赖可能被多个包依赖，去重后再展示
        used = sorted({(d, ) for _, d in broken})
        log("断环", f"这些包由构建环境提供，指向它们的 {len(broken)} 条依赖边被断掉：")
        for (dep,) in used:
            who = sorted({n for n, d in broken if d == dep})
            print(f"    {dep}  ← 被 {', '.join(who)} 依赖")
        print()
        print("    它们【仍然会被构建】——断掉的只是「等待」，不是「构建」。")
        print("    自举的目标正是重建它们。")
    else:
        print()
        log("断环", "没有需要断的边：依赖图本身无环")
