"""构建顺序：按依赖拓扑排序，环断在构建环境基线上。

## 为什么不能只做拓扑排序

35 个配方之间有 108 条依赖边，其中有一个**真的环**：

    glibc 的 build 依赖 binutils 和 gcc
    gcc   的 runtime 依赖 glibc
    glibc → binutils → gcc → glibc

所以顺序**不是配方的纯函数**——它取决于构建环境已经提供什么。这正是
`docs/决策记录/0011` 说的那件事：`bootstrap/provides.txt` 里的包由构建环境满足，
指向它们的边断在那里，环就开了。

## 一个容易搞错的地方

基线里的包**仍然要构建**，只是**不等待**。这两件事是分开的：

    依赖边    →  断掉（不等待它先建好）
    构建列表  →  保留（自举的目标就是重建它们）

如果把基线包整个从列表里去掉，就变成「因为 Debian 提供了 glibc，所以永远
不重建 glibc」——那和自举的方向正好相反。
"""

from __future__ import annotations

from collections import deque

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


def _topo_sort(graph: dict[str, set[str]],
               names: list[str]) -> tuple[list[str], list[str]]:
    """确定的拓扑排序。返回 (顺序, 排不进去的节点)。

    ★ 为什么不用 graphlib.TopologicalSorter。

      它内部用 set 存前驱和后继，于是**迭代顺序取决于 Python 的字符串 hash
      随机化**——每个进程都不一样。实测同一份输入连跑三次给出三种顺序：

          $ ./skirt/skirt order | md5     # 三次三个值

      而 `skirt order` 这个子命令存在的意义正是「让重建顺序可复现」（见
      docs/待办.md 里「重建顺序只存在于我执行过的那条命令里」那条）。一个
      每次都不一样的"可复现顺序"是自相矛盾的。

      【判据】别凭直觉，做对照：固定 PYTHONHASHSEED 之后跑两次完全一致，
      换一个 seed 就不一样了。所以就是它，不是文件系统顺序之类的东西。

      这里是 Kahn 算法。入队顺序由两样东西决定：

        names        —— 调用方给的列表，all_recipe_names() 已经排过序
        succ[n]      —— 按 names 的顺序 append 出来的，所以也是升序

      两者都不碰 hash，所以输出是输入的纯函数。

    ★ 为什么不用 sorted() 在最后补一刀。

      那只能让【同一个顺序】看起来稳定，改不了"哪一步先走"取决于 set 迭代
      这件事。要确定就得从遍历本身确定。
    """
    indeg = {n: 0 for n in names}
    succ: dict[str, list[str]] = {n: [] for n in names}

    # names 已排序，所以 succ[p] 里装的是升序的 n——这一步是确定的。
    # （graph[n] 是 set，迭代顺序随机；但每个 p 至多出现一次，所以随机的只是
    #   "此刻往哪个 p 的列表里加"，而每个列表最终的内容和顺序都由 n 的升序决定。）
    for n in names:
        for p in graph.get(n, ()):
            if p not in indeg:
                continue
            indeg[n] += 1
            succ[p].append(n)

    ready = deque(n for n in names if indeg[n] == 0)
    out: list[str] = []
    while ready:
        n = ready.popleft()
        out.append(n)
        for s in succ[n]:
            indeg[s] -= 1
            if indeg[s] == 0:
                ready.append(s)

    done = set(out)
    return out, [n for n in names if n not in done]


def _find_cycle(graph: dict[str, set[str]], stuck: list[str]) -> list[str]:
    """从 stuck 里挑一条具体的环出来，为了把报错说清楚。

    排不进去的节点每个都还有前驱，而那些前驱也一定排不进去（排进去的话它的
    后继就会被减到 0）。所以沿着前驱走一定留在 stuck 里，也一定会绕回来。
    """
    stuck_set = set(stuck)
    seen: dict[str, int] = {}
    path: list[str] = []
    n = stuck[0]
    while n not in seen:
        seen[n] = len(path)
        path.append(n)
        nxt = [p for p in graph.get(n, ()) if p in stuck_set]
        if not nxt:                     # 走不到这儿；真到了就退回一个能看懂的
            return stuck
        n = nxt[0]
    return path[seen[n]:] + [n]


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

    order, stuck = _topo_sort(graph, names)
    if stuck:
        cycle = _find_cycle(graph, stuck)
        die(
            "依赖成环，排不出构建顺序：\n"
            f"  {' → '.join(cycle)}\n"
            "\n"
            "环必须由构建环境提供一个起点。把环上最底层的那一个加进\n"
            "bootstrap/provides.txt（它会由构建环境满足，不再等待先建好），\n"
            "然后在构建环境里真的提供它。\n"
            "\n"
            f"当前的基线（bootstrap/provides.txt）: {sorted(host_provides) or '（空）'}"
        )

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
