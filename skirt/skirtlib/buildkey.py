"""build_key —— 一个包的身份，从内容算出来，而不是从人写的版本号来。

【它解决什么】

一个包的身份现在是 `<名字>-<版本>-<release>`，而 `release` 是人写的。于是
"内容变了"这件事**没有客观依据** —— 全靠写的人记得升那个号。忘了会怎样：

    CI 的跳过判断是「var/pkgs 里有没有同名同版本同 release 的 .apk」
    → 你没升 release
    → 它说「跳过 X（已经有了）」
    → 发布出去的是旧产物
    → 而索引、自检、发布全都正常

实测撞过：`yojigen-os-init` 里加了 `cttyhack`、也重建发布了，但版本还是
`1.0-r1` —— 新装的机器有，老机器升级升不出来。

【它是什么】

把**所有决定产物的东西**算成一个哈希：

| 成分 | 为什么 | 实证 |
|---|---|---|
| 配方内容（spec + steps + 补丁 + files/ + scripts/） | 显而易见 | — |
| `SOURCE_DATE_EPOCH` | 0005 | — |
| **构建环境** | Debian 环境 vs 自举环境产出不同 | GCS 位那次：同配方、同 gcc，产物不同 |
| **驱动本身** | 驱动决定输出 | `--libdir` 那次：只改驱动，gcc 的行为就变了 |
| 架构 | | — |
| **依赖的 build_key** | 递归的链 | — |

后两项是**实测踩出来的**，而不是想出来的。而它们的意义在于：**键里少一样，
比没有键更危险** —— 少了它就不会重建，而产物其实变了，于是你悄悄产出一批
新旧混着的东西，而没有任何东西会告诉你。

**所以：加了东西就要加进这里；拿不准的一样要加。**

【怎么用】

    · 每次构建算一次，写进 `meta.json`，也在产物目录里留一份 `.buildkey`
      （旁路文件 —— 因为 `apk mkpkg` 不收自定义字段，key 进不了 .apk）
    · `skirt stale <包>` 拿现在的 key 和盘上那个比 —— 不一样就该重建
    · 一样的话，说明"内容、环境、驱动、依赖"都没变

而它顺带治掉另一半：**因为链是递归的，依赖变了用它的包也会被标出来。** 今天
那件事靠人记得把依赖也列进 `packages=`。

【拿不准的时候】

某一成分取不到（比如 Docker 不在、构建环境镜像还没建），就标成 `unknown`，
而 `needs_rebuild()` 把 unknown 当成"必须重建"。**取不到就说取不到，别猜。**
"""

from __future__ import annotations

import ast
import hashlib
import json
import subprocess
from pathlib import Path

from . import common
from .common import ROOT, sha256_of

#: 产物目录里那份旁路文件的名字。它在 `var/output/<架构>/<名字>-<版本>-<release>/` 下。
KEY_FILE = ".buildkey"

#: 取不到的成分用这个值。`needs_rebuild()` 见到它就要求重建。
UNKNOWN = "unknown"


# ------------------------------------------------------------------ 哈希

def _mix(h: "hashlib._Hash", *chunks: str) -> None:
    """把一个字符串喂进去，带分隔符。

    分隔符是必需的：不加的话 ("ab", "c") 和 ("a", "bc") 会算出同一个哈希。
    """
    for c in chunks:
        h.update(c.encode("utf-8"))
        h.update(b"\0")


def recipe_hash(recipe_dir: Path) -> str:
    """配方目录里所有文件的内容。

    按相对路径排序，逐个把「路径 + 内容哈希」喂进去 —— 所以改名、加文件、
    删文件、改内容，都会变。

    跳过 `__pycache__`：它是解释器的产物，不是配方的一部分。
    """
    h = hashlib.sha256()
    for p in sorted(recipe_dir.rglob("*")):
        if p.is_dir() or "__pycache__" in p.parts:
            continue
        _mix(h, p.relative_to(recipe_dir).as_posix(), sha256_of(p))
    return h.hexdigest()


def _strip_docstrings(tree: ast.AST) -> ast.AST:
    """把文档字符串去掉。

    为什么在意它：文档里明确写着「**只改注释**不用升 release（产物一个字节
    都不变）」。而按源码的字面哈希，改一行注释就要全量重建 —— 那是纯粹的浪费。

    注释在 AST 里本来就不存在，所以 `ast.unparse` 自然不带它；要单独处理的是
    **文档字符串**，那个是真实的 AST 节点。
    """
    holders = (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
    for node in ast.walk(tree):
        if not isinstance(node, holders):
            continue
        body = node.body
        if (body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            # 不能留空 body —— 那会 unparse 出一个语法错的东西
            node.body = body[1:] or [ast.Pass()]
    return tree


def driver_hash() -> str:
    """驱动自己的内容 —— 注释和文档字符串剥掉之后。

    为什么驱动要进这个键：实测过，**只改驱动就能改变产物** —— 那次是给驱动
    加 `--libdir`，同一个 gcc 的行为就变了。所以「同配方、同 gcc」不等于
    "同产物"。

    而为什么剥注释：见 `_strip_docstrings`。剥不掉的就退回原始字节，
    并且**说出来** —— 那意味着这一个文件会比它实际需要的变化得更频繁。
    """
    files = sorted(
        p for p in (ROOT / "skirt").rglob("*.py")
        if "__pycache__" not in p.parts
    )
    h = hashlib.sha256()
    for p in files:
        src = p.read_text(encoding="utf-8")
        try:
            norm = ast.unparse(_strip_docstrings(ast.parse(src)))
        except SyntaxError:
            norm = src
            print(f"  ⚠ build_key: {p.relative_to(ROOT)} 解析不了，用原始字节"
                  f"（意味着注释也算进去）")
        _mix(h, p.relative_to(ROOT).as_posix(), hashlib.sha256(
            norm.encode("utf-8")).hexdigest())
    return h.hexdigest()


def buildenv_id(arch: str) -> str:
    """构建环境镜像的内容 Id。

    `docker image inspect` 给的 Id 就是镜像内容本身的哈希，所以同样的内容
    给同样的值 —— 这正是我们要的。

    取不到就返回 UNKNOWN（不猜）。什么时候会取不到：Docker 不在、或者镜像
    还没建（自举那条线上，镜像是这一轮自己建出来的）。
    """
    # ★ 读【调用时】的那个值，而不是导入时快照下来的那份。
    #
    #   原来写的是 `from .common import BUILDENV_IMAGE` —— 那是在导入时复制一份，
    #   于是之后改 `SKIRT_BUILDENV`（或者测试里 monkeypatch）都不会生效。
    #   实测撞到过：同一个 python 进程里改了环境变量、reload 了本模块，而它
    #   读到的还是旧值 —— 因为 common 那个模块没跟着 reload。
    #
    #   这正是这个项目反复强调的那个形状：**两个同名但不是同一个东西**。
    image = f"{common.BUILDENV_IMAGE}:{arch}"
    try:
        r = subprocess.run(
            ["docker", "image", "inspect", "--format", "{{.Id}}", image],
            capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return UNKNOWN
    if r.returncode != 0 or not r.stdout.strip():
        return UNKNOWN
    return r.stdout.strip()


# ------------------------------------------------------------------ 算

def compute(spec: dict, arch: str, epoch: int, dep_keys: list[tuple[str, str]]) -> dict:
    """算一个包的 build_key。返回 {"key": …, "parts": {…}}。

    `parts` 也留着，而且写进 meta.json —— **键算错了要能查**。只有一个光秃秃
    的哈希的话，你只能看着它说"怎么变了"，而看不出来是哪一样变的。
    """
    parts = {
        "arch": arch,
        "recipe": recipe_hash(spec["_dir"]),
        "epoch": str(epoch),
        "buildenv": buildenv_id(arch),
        "driver": driver_hash(),
        # 依赖按名字排序 —— 闭包的顺序由依赖图决定，而那是另一个变量，
        # 不该影响"这个包的内容是什么"
        "deps": {n: k for n, k in sorted(dep_keys)},
    }
    return {"key": key_of(parts), "parts": parts}


# ------------------------------------------------------------------ 存和读

def stored_parts(pkg_dir: Path) -> dict | None:
    """读盘上那个包上次的 build_key 成分。

    两个来源，`meta.json` 优先：
      · `meta.json` 的 `build_key_parts` —— 本次构建刚写的
      · `.buildkey` —— 旁路文件，为了跨 CI 的运行（题记里说了为什么要有它）
    """
    meta = pkg_dir / "meta.json"
    if meta.is_file():
        try:
            d = json.loads(meta.read_text(encoding="utf-8"))
            if isinstance(d.get("build_key_parts"), dict):
                return d["build_key_parts"]
        except (OSError, json.JSONDecodeError):
            pass
    side = pkg_dir / KEY_FILE
    if side.is_file():
        try:
            d = json.loads(side.read_text(encoding="utf-8"))
            if isinstance(d.get("parts"), dict):
                return d["parts"]
        except (OSError, json.JSONDecodeError):
            pass
    return None


def write_sidecar(pkg_dir: Path, computed: dict) -> None:
    """在产物目录里留一份 —— 它跟着 `var/pkgs` 的缓存走，所以 CI 跨运行也有。"""
    pkg_dir.mkdir(parents=True, exist_ok=True)
    (pkg_dir / KEY_FILE).write_text(
        json.dumps(computed, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")


def stored_key_of(pkg_dir: Path) -> str | None:
    """盘上那个包的 build_key（没有就 None）。"""
    parts = stored_parts(pkg_dir)
    if parts is None:
        return None
    return key_of(parts)


def key_of(parts: dict) -> str:
    """从成分算出 key —— 和 `compute()` 用的是同一段代码，所以不会分叉。"""
    blob = json.dumps(parts, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def needs_rebuild(now: dict, before: dict | None) -> tuple[bool, list[str]]:
    """要不要重建，以及**为什么**。

    返回 (要重建?, 变了的成分名列表)。

    ★ 拿不准就重建：
      · 盘上没有（`before is None`）—— 没建过
      · 这一次有成分是 UNKNOWN —— 算不准
      · 上一次有成分是 UNKNOWN —— 上次就没算准，没法比
    """
    if before is None:
        return True, ["（盘上没有它上次的 build_key）"]

    a, b = now["parts"], before
    if not isinstance(b, dict):
        return True, ["（上次那份记的不是个字典，看不懂）"]

    changed = [k for k in sorted(set(a) | set(b)) if a.get(k) != b.get(k)]

    if UNKNOWN in (str(a.get("buildenv")), str(b.get("buildenv"))):
        changed.append("buildenv=unknown（取不到构建环境的身份，那就当它变了）")

    return bool(changed), changed
