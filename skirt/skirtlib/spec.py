"""配方：加载 spec.toml、展开 $NAME/$VERSION 占位符、解析依赖字段、构建时间戳。"""

from __future__ import annotations

import datetime
import tomllib
from functools import lru_cache

from .common import RECIPES_DIR, die, log

# ------------------------------------------------------------------ 配方

def spec_path(name: str) -> Path | None:
    """按名字找配方的路径，找不到返回 None。

    **配方目录按 kind 分层**（recipes/lib/glibc/spec.toml），所以不能直接
    `RECIPES_DIR / name`。这里也认平铺的老结构——迁移期间两种都得能读。

    kind 的判据和分类见 docs/约定/配方约定.md「包分类」一节。
    """
    flat = RECIPES_DIR / name / "spec.toml"
    if flat.is_file():
        return flat
    for kind_dir in sorted(RECIPES_DIR.iterdir()):
        if not kind_dir.is_dir() or kind_dir.name.startswith("."):
            continue
        cand = kind_dir / name / "spec.toml"
        if cand.is_file():
            return cand
    return None


def kind_of(name: str) -> str:
    """这个包属于哪一类。分类就是它所在的目录名。

    **分类只有一处**：目录。不再有一个 kind 字段和它并存——两处就会不一致。
    """
    path = spec_path(name)
    if path is None:
        return "?"
    rel = path.relative_to(RECIPES_DIR)
    return rel.parts[0] if len(rel.parts) > 2 else "?"


def all_recipe_names() -> list[str]:
    """所有配方名。目录按 kind 分层，所以要多下钻一层。"""
    out: list[str] = []
    for d in RECIPES_DIR.iterdir():
        if not d.is_dir() or d.name.startswith("."):
            continue
        if (d / "spec.toml").is_file():
            out.append(d.name)                      # 平铺（迁移期间）
            continue
        out += [c.name for c in d.iterdir()
                if c.is_dir() and (c / "spec.toml").is_file()]
    return sorted(out)


# ★ `[build]` 段认识哪些键。这份清单必须和 `build.py` 里真正读的键【一致】——
#   而"一致"这件事没有自动保证，所以两处都在注释里互相指着。
#
#   ★ 2026-10-20 加了 `vendor`（决策记录 0022）：它标记"这个包来自上游二进制，
#   不分发源码、不编译"。而它的用处是让 5.12.2/5.12.3 那两条检查知道
#   【该检谁】——见 docs/待办.md §5.12。
#   ★ `vendor_check`（2026-10-20 加，决策记录 0022 的 5.12.3）：
#   对分发包，"它真的能跑"这件事要在【装好的系统里】验 —— 那是 distro 那边
#   的事（`bootstrap.yml` 的自检那一步，它已经会装一个系统）。
#
#   而那个命令【不该由 CI 猜】：大多数分发包都能用 `<包名> --version`，
#   而有些不能（`docker` 的包名和程序名不同、`1panel` 是个服务、`rust` 装的是
#   一堆二进制）。所以在配方里写：
#
#       vendor_check = "docker --version"
#
#   不写就用默认的 `<包名> --version` —— 而【写了会覆盖它】。
BUILD_KEYS = frozenset({
    "type",           # autotools / cmake / meson / custom
    "autoreconf",     # 要不要先跑 autoreconf
    "cflags",
    "configure",      # 交给 configure/cmake/meson 的参数（★ 会【追加】在默认值之后）
    "nostrip",
    "out_of_tree",
    "patches",
    "vendor",         # ★ "这个包来自上游二进制"，值是一句人能读的说明
    "vendor_check",   # ★ 可选：怎么验它真的能跑（5.12.3）。见下
})


def load_spec(name: str) -> dict:
    path = spec_path(name)
    if path is None:
        die(f"找不到配方: {name}（找过 recipes/<kind>/{name}/spec.toml）")
    if not path.is_file():
        die(f"找不到配方: {path}")
    with path.open("rb") as f:
        spec = tomllib.load(f)
    if "package" not in spec:
        die(f"{path}: 缺少 [package] 段")
    for key in ("name", "version"):
        if key not in spec["package"]:
            die(f"{path}: [package] 缺少 {key}")

    # ★★★ [build] 的键白名单 —— 2026-10-20 加。
    #
    #   在这之前，[build] 里【多写一个键不会报错，会被静静忽略】：
    #   build.py 只读 type / autoreconf / cflags / configure / nostrip /
    #   out_of_tree / patches，而 spec.py 从不检查还有没有别的。
    #
    #   而那是这个项目一直在修的那一类 —— 一个该大声失败的地方静静地成功了。
    #   实测的代价会是这样：有人写 `patch = [...]`（少个 s），配方照建，
    #   补丁没打，而产物看起来正常。**而那个人会以为自己打了补丁。**
    #
    #   ★ 而这一条在加 `vendor` 的时候顺手加，是因为那时正好在改这里；
    #     而它会立刻抓到现有配方里写错的键（如果有）。
    for key in (spec.get("build") or {}):
        if key not in BUILD_KEYS:
            die(f"{path}: [build] 里有一个不认识的键 {key!r}\n"
                f"          认识的只有：{', '.join(sorted(BUILD_KEYS))}\n"
                f"          （多写的键会被静静忽略，所以这里直接报错）")

    spec["_dir"] = path.parent
    return spec


def expand(spec: dict, value, arch: str | None = None):
    """展开 $NAME / $VERSION / $ARCH 占位符。

    ★ $ARCH 是后加的，起因是一个具体的需求：有些 configure 选项必须跟着目标
      架构走，而 spec.toml 里没办法表达它。

        apk-tools 的 -Darch=aarch64    ← 它决定 apk 去仓库的哪个子目录取包
        bash 的 --host=aarch64-yojigenos-linux-gnu
                                      ← 它决定 bash --version 显示什么

      第一版是把 aarch64 写死在 spec.toml 里。那在只建 aarch64 时看不出来，
      但 x86_64 的配方会静静地建出一个认错架构的 apk——而它报的错和"仓库里
      没有这个包"长得一样，很难查。

    ★ arch 是可选参数而不是从 spec 里取：load_spec 不知道目标架构，那是调用
      者的事。传 None 时 $ARCH 展开成空字符串——和"没定义这个变量"一致。
    """
    pkg = spec["package"]
    if isinstance(value, str):
        return (value
                .replace("$NAME", pkg["name"])
                .replace("$VERSION", pkg["version"])
                .replace("$ARCH", arch or ""))
    if isinstance(value, list):
        return [expand(spec, v, arch) for v in value]
    if isinstance(value, dict):
        return {k: expand(spec, v, arch) for k, v in value.items()}
    return value


#: 依赖段里【只该有】这两个键 —— 架构名。别的一律是错的。
ARCH_KEYS = ("aarch64", "x86_64")


def deps_for(spec: dict, arch: str) -> dict:
    """这个配方在【这个架构】上的依赖。**必须显式写出该架构那一份。**

    ★ 2026-10-15 改过一次，而改的是语义 —— 原来的写法是「通用 + 架构覆盖」：

        deps = dict(spec["dependencies"])
        per_arch = deps.pop(arch, None)
        if isinstance(per_arch, dict):
            deps.update(per_arch)          ← 键级覆盖

    那个写法有两个洞，而两个都在真实数据里发作了：

    **① 覆盖是【整键】的，所以"少写一个键"等于"那个键是空的"。**
        `zlib` 和 `hello` 的 `x86_64` 段只写了 `runtime`，于是它们的 `build`
        被盖成空 —— x86_64 上**没有** build 依赖。而包照样建得出来（构建环境里
        本来就有那些），所以一直没人看见。后果在 `build_key` 上：x86_64 的 key
        里不含那些，它们变了不会触发重建。

    **② 另一个架构的那份会【漏进结果】。**
        `deps_for(spec, "aarch64")` 返回的字典里带着一个 `x86_64` 键 ——
        因为 `pop(arch)` 只摘掉自己那一份，别人的留着。

    **所以现在改成：两个架构各写一份，没有"通用"。**
    于是"改了通用那行而只对一个架构生效"这件事在结构上不可能发生
    —— 因为它没有"通用"可以改。

    ★ 而通用段现在会被【明确拒绝】，不是被忽略：留着一段没人读的依赖，
      正是"改了不生效"最容易生根的地方。
    """
    所有 = spec.get("dependencies") or {}
    通用 = [k for k in 所有 if k not in ARCH_KEYS]
    if 通用:
        raise ValueError(
            f"依赖段里有非架构键：{通用} —— "
            f"按约定，两个架构各写一份（{[f'[dependencies.{a}]' for a in ARCH_KEYS]}），"
            f"没有「通用」那一段"
        )
    这份 = 所有.get(arch)
    if not isinstance(这份, dict):
        raise ValueError(f"[dependencies.{arch}] 不在（两个架构都要显式写）")
    # ★ 连里面的列表也拷一层。浅拷贝看着够用，而调用方 `append` 一下就
    #   会把 spec 里那份改掉 —— 而 spec 是缓存着的。
    return {k: list(v) if isinstance(v, list) else v for k, v in 这份.items()}


def direct_deps(spec: dict, arch: str,
                kinds: tuple[str, ...] = ("runtime", "build")) -> list[str]:
    """直接依赖：去重且保持声明顺序。

    默认取 runtime + build（构建一个包时两者都要）。拼运行时的根文件系统
    时传 kinds=("runtime",)——构建依赖不该进最终系统：glibc 的构建依赖里
    有 gcc，但它运行起来不需要编译器。
    """
    d = deps_for(spec, arch)
    out: list[str] = []
    for key in kinds:
        for name in d.get(key, []):
            if name not in out:
                out.append(name)
    return out


# ------------------------------------------------------------ 构建时间戳

def package_date(spec: dict) -> int:
    """读取配方声明的日期（`[package] date`），返回 Unix 秒。

    这个日期必须**写在配方里**，不能从 git 提交时间或文件 mtime 推导。
    那些是「关于内容的元数据」：改动还没提交时它们指向旧值，提交之后才
    变成新值——于是同一个配方在本地和 CI 上会算出不同的时间戳，每次提交
    都产出一份不同的字节。写进文件里，它就随同一次提交一起生效，
    提交前和提交后一致。

    对应 Debian 的 debian/changelog 和 Fedora 的 %changelog：**日期是源码
    的一部分，不是源码的属性。**
    """
    pkg = spec["package"]
    name = pkg.get("name", "?")

    if "date" not in pkg:
        die(f"{name}: [package] 缺少 date\n"
            f"       它是 SOURCE_DATE_EPOCH 的来源，必须写在配方里。\n"
            f"       格式（TOML 原生日期时间，不要加引号）:\n"
            f"           date = 2026-10-10T15:30:00Z")

    value = pkg["date"]
    if not isinstance(value, datetime.datetime):
        die(f"{name}: date 必须是 TOML 日期时间，收到 {value!r}\n"
            f"       加了引号就会被当成普通字符串。去掉引号：\n"
            f"           date = 2026-10-10T15:30:00Z\n"
            f"       而不是 date = \"2026-10-10T15:30:00Z\"")

    if value.tzinfo is None:
        die(f"{name}: date 必须带时区\n"
            f"       否则同一个时刻在不同时区会算出不同的时间戳。\n"
            f"           date = 2026-10-10T15:30:00Z          （UTC）\n"
            f"           date = 2026-10-10T23:30:00+08:00      （北京时间）")

    return int(value.timestamp())


def latest_date(spec: dict, dep_specs: list[dict]) -> int:
    """本包的 SOURCE_DATE_EPOCH：自己与**所有依赖**里最晚的那个日期。

    为什么不只看自己：配方一个字没改、但依赖更新了，产出的字节**确实**
    会变——链接进去的库换了。时间戳要是纹丝不动，就会产出「同名、同 mtime、
    内容不同」的包，而那正是所有靠 mtime 判断变化的同步与备份工具的杀手。

    Debian 到今天还在靠人在 binNMU 时手工把时间戳加一来绕开这件事。让时间戳
    跟着依赖走，它就自动对了：依赖的日期一变，所有下游跟着变。

    闭包里的环（glibc → make → glibc 是自举的常态）由调用方处理，
    这里只消费结果。
    """
    mine = spec["package"]["name"]
    best, origin = package_date(spec), mine

    for dep in dep_specs:
        d = package_date(dep)
        if d > best:
            best, origin = d, dep["package"]["name"]

    if origin != mine:
        log("时间戳", f"SOURCE_DATE_EPOCH 取自依赖 {origin}（它比本包的 date 更晚）")
    return best


# ------------------------------------------------------------------ 子包

def subpackages(spec: dict) -> list[dict]:
    """配方声明的子包：同一份源码产出的额外包。

    ```toml
    [[subpackage]]
    name        = "glibc-dev"
    description = "glibc 开发文件：头文件、静态库"
    files = ["usr/include/", "usr/lib/*.a"]
    ```

    拆分发生在**收集阶段**——构建脚本照旧把一切装进 $PKGDEST，这里只决定
    收集时每个文件归谁。好处是改拆分规则不用重编：跑一次 collect 就够。
    """
    subs = spec.get("subpackage", [])
    if not isinstance(subs, list):
        die(f"{spec['_dir']}/spec.toml: [[subpackage]] 必须是数组")
    for sub in subs:
        where = f"{spec['_dir']}/spec.toml"
        for key in ("name", "files"):
            if key not in sub:
                die(f"{where}: [[subpackage]] 缺少 {key}")
        if not isinstance(sub["files"], list) or not sub["files"]:
            die(f"{where}: 子包 {sub['name']} 的 files 不能为空")
        if sub["name"] == spec["package"]["name"]:
            die(f"{where}: 子包名不能和主包同名（{sub['name']}）")
    return subs


@lru_cache(maxsize=1)
def _recipe_names() -> tuple[str, ...]:
    return tuple(all_recipe_names())


@lru_cache(maxsize=1)
def _subpackage_owners() -> dict[str, str]:
    owners: dict[str, str] = {}
    for recipe in _recipe_names():
        for sub in subpackages(load_spec(recipe)):
            owners[sub["name"]] = recipe
    return owners


def recipe_of(name: str) -> str | None:
    """把包名解析成**配方名**，不认识则返回 None。

    普通包返回自己；子包返回产出它的那个配方（`glibc-dev` → `glibc`）。
    子包没有自己的目录——它只存在于 var/output/ 里，所以任何「按名字找配方」
    的地方都得先过这一层。
    """
    if spec_path(name) is not None:
        return name
    return _subpackage_owners().get(name)


def is_subpackage(name: str) -> bool:
    return (spec_path(name) is None and name in _subpackage_owners())


def package_rel(name: str, version: str, release: int) -> str:
    """output 目录名，也是日志里显示的标识。"""
    return f"{name}-{version}-{release}"
