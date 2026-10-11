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

    kind 的判据和分类见 docs/配方约定.md「包分类」一节。
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
    spec["_dir"] = path.parent
    return spec


def expand(spec: dict, value):
    """展开 $NAME / $VERSION 占位符。"""
    pkg = spec["package"]
    if isinstance(value, str):
        return (value
                .replace("$NAME", pkg["name"])
                .replace("$VERSION", pkg["version"]))
    if isinstance(value, list):
        return [expand(spec, v) for v in value]
    if isinstance(value, dict):
        return {k: expand(spec, v) for k, v in value.items()}
    return value


def deps_for(spec: dict, arch: str) -> dict:
    """合并通用依赖与架构特定依赖（架构特定值整体覆盖同名键）。"""
    deps = dict(spec.get("dependencies", {}))
    per_arch = deps.pop(arch, None)
    if isinstance(per_arch, dict):
        deps.update(per_arch)
    return deps


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
