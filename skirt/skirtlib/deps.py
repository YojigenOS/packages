"""依赖解析：构建环境基线、依赖闭包、payload 定位、sysroot 叠加。"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from .common import BUILD_DIR, HOST_PROVIDES_FILE, OUTPUT_DIR, ROOT, die, log, rmtree_force

from .spec import direct_deps, latest_date, load_spec, package_rel, recipe_of

# ---------------------------------------------------------------- 依赖解析

def load_rebuild_only() -> set[str]:
    """哪些包【明明在构建环境里、却仍然要重建】（2026-10-20 加）。

    ★ 为什么需要它 —— §1.5.Bf 那个结构性问题的修法：

      `SKIRT_PROVIDES` 原来一个集合干了两件事：

        ① `build_graph` 里把指向这个包的边【断掉】—— 那是【解决依赖环】的机制
        ② 同时让它【不进构建顺序、永远不重建】

      对 autoconf / make / cmake 这类【纯构建工具】，两件事都对。
      而对 `readline` / `ncurses` / `openssl` / `zlib` 这类【既要进构建环境、
      又要装进用户系统】的包，② 是错的：它们被"环境提供"之后就再也【不会
      跟着配方更新】，而最终系统里装的是那个永不更新的版本。

      实测（2026-10-18 连续四轮）：readline 的 build 依赖补了 ncurses，而
      config 改了【不生效】—— 因为 readline 那一轮根本没被建。于是 python3
      链上镜像里那份旧的 libreadline.so.8：

          [ERROR] readline failed to import: …: undefined symbol: UP

    ★ 所以现在分成两件事：

        SKIRT_PROVIDES   ① 断环（不变，还是完整闭包）
        SKIRT_REBUILD    ★ 这个集合里的包【也进构建顺序】

      而 ① 依然有效：那个 `make → glibc → python3 → zlib → make` 的环还是
      靠 `make` 在 `SKIRT_PROVIDES` 里、而不在 `SKIRT_REBUILD` 里断掉的
      （`make` 永远不交付给用户，所以它不该重建）。

    ★ 而这里【不加默认值】。空集合是合法的（= 不重建任何"本该跳过"的包，
      旧行为）。而"哪 33 个该重建"是 CI 算的（`devel` 闭包减 `base`+`minimal`
      闭包），不该在 packages 里猜 —— 那和镜像名不该出现在 recipes 里是
      同一条分界。
    """
    env = os.environ.get("SKIRT_REBUILD", "").strip()
    out: set[str] = set()
    for chunk in env.replace(",", " ").split():
        if chunk:
            out.add(chunk)
    return out


def load_host_provides() -> set[str]:
    """构建环境已经提供了哪些包。

    两个来源，按优先级：

    ★ SKIRT_PROVIDES —— 环境变量的内容，逗号或空白分隔。

      用在「构建环境是我们自己的 devel」时：那个环境里有的是
      variants/devel.toml 里列的那些包，而不是 bootstrap/provides.txt
    （★ 这里原来写着「那 39 个包」—— 2026-10-15 核的时候 devel 已经是 43 个了。
     所以数字去掉了：**它算得出来，就不该靠人记得**。）
      里那四项。那四项是给【借来的 Debian 起点】写的。

      为什么不写死一个文件：因为 devel 的定义在 distro 仓库里（风味定义），
      而这里是 packages。抄一份过来就是"同一个东西两个出处"——项目里已经
      为这个吃过亏。所以让它从外面传进来。

    bootstrap/provides.txt —— 默认。借来的起点提供什么。

    ★ 两者的语义差别是实的：
        Debian 起点  它提供的东西有限，必须显式列出（还要处理环）
        我们自己的 devel  里面每个包都是我们建的，列出来就是它的包集合
    """
    env = os.environ.get("SKIRT_PROVIDES", "").strip()
    if env:
        out = set()
        for chunk in env.replace(",", " ").split():
            if chunk:
                out.add(chunk)
        return out

    if not HOST_PROVIDES_FILE.is_file():
        return set()
    out = set()
    for line in HOST_PROVIDES_FILE.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            out.add(line)
    return out


#: 这些包【永远不】因为 `SKIRT_REBUILD` 而进构建 sysroot。
#
#  ★★★ 2026-10-21 加。它们是构建容器自己的**命根子**：
#
#    glibc       —— 动态链接器（`usr/lib64/ld-linux-x86-64.so.2`）和 libc 本身。
#                    `build.py` 那段合并注释已经写过一次：「Links 一改，链接器
#                    就悬空了 —— 容器里所有动态链接的程序立刻全废」。
#    filesystem  —— 容器的目录布局（`/lib64 -> usr/lib64` 这类 symlink 就在它里面）。
#
#  ★ 而今天把它们合并进去**确实无害**：`buildenv` 换成我们自己的 devel 镜像之后，
#    镜像里那份和这一轮重建的是【逐字节相同】的（可复现构建）。但那是"今天恰好"，
#    不是"永远" —— 而它坏起来的样子是"容器里所有程序突然都不能跑"，
#    查起来极贵（历史上查过一次）。
#
#  ★★ 所以规则是：这两条不进 sysroot，其余 28 个进的。判据是"它在不在
#     `build.py` 那段命根子清单里"，不是"它重不重要"。
SYSRROOT_NEVER = frozenset({"glibc", "filesystem"})


def dep_closure(spec: dict, arch: str, host_provides: set[str],
                kinds: tuple[str, ...] = ("runtime", "build"), *,
                include_rebuild: bool = False) -> list[str]:
    """返回按安装顺序排列的依赖包名（依赖在前，不含自身，已剔除宿主提供的）。

    ★★★ `include_rebuild`（2026-10-21 加）—— 那条一直没修的另一半。

    `SKIRT_REBUILD` 里的包（既在构建环境里、又是要交付的）会【被重建】，而
    `order.py` 也已经让它们的依赖边回来了（B1）。但这里的跳过是另一处：
    `if name in host_provides` 一把全跳过，于是**重建了却没人用它**。

    实测过的后果：`python3` 的「需自建」闭包在 CI 那 62 个 `PROVIDES` 下是
    **空的** —— 它的构建容器里一个依赖都没有，链接和 import 拿的全是镜像里
    那份。而 readline 那次就是坏在镜像里那份上。

    ★ 只在「准备构建 sysroot」那条路上打开（`commands.cmd_build`）。别的地方
      （`provides --runtime`、`source_date_epoch`）保持原语义 —— 那里的"闭包"
      回答的是另一个问题。

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

    # ★ 只有"准备构建 sysroot"那条路会打开它；而打开之后仍然绕过 SYSRROOT_NEVER。
    reborn = load_rebuild_only() - SYSRROOT_NEVER if include_rebuild else set()

    def visit(name: str) -> None:
        if name in done:
            return
        if name in host_provides and name not in reborn:
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


def output_dir(name: str, arch: str) -> tuple[Path, str]:
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
    payload = output_dir(name, arch)[0] / "payload"
    return payload if payload.is_dir() else None


def dep_payload(name: str, arch: str) -> tuple[Path, str]:
    """找到依赖包已构建的 payload。"""
    out, rel = output_dir(name, arch)
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


