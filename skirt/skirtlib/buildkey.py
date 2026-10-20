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
| **构建环境** | Debian 环境 vs 自举环境产出不同 | **13 字节那次**（2026-10-15 查到底）：
同配方、同 gcc、同 binutils，只换环境里那份 glibc —— 段表逐项相同、大小相同
（67824 vs 67824），逐字节比正好差 13 |
| **工具集**（驱动 + `scripts/`） | 它们决定输出 | `--libdir` 那次：只改驱动，gcc 的行为就变了 |
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

    【`spec.toml` 特殊对待：按语义哈希，不按字节】

    其余文件都是原始字节，而 `spec.toml` 先经 `tomllib` 解析、再按规范化 JSON
    哈希。于是**改它的注释和排版不会进这个哈希**。

    为什么值得这么麻烦：这个项目的配方**注释占大头**（实测 openssh 那个配方
    245 行里 180 行是注释，73%）。而约定文档里写着「**只改注释**不用升
    `release`（产物一个字节都不变）」—— 按字节哈希的话，那句话就变成了假的：
    改一行注释要白建一次。

    ★ 而这里用 `tomllib` 是真解析器，不是"用正则剥 `#`"——**不会少算**。
      这是它和下面 `steps.sh` 那个选择的关键区别。

    【而 `steps.sh` 保持原始字节】

    剥 shell 的注释要懂 heredoc、引号里的 `#`、续行 —— 而**剥错的后果是漏掉
    一次真实改动**，那比多算几次严重得多。所以那边选保守那头：改注释也会
    重建一次，产物字节一样。

    ★ 于是"只改注释不用重建"这句话，对 `spec.toml` 成立，对 `steps.sh` 不成立。
      精确的说法写进 `docs/约定/配方约定.md` 了 —— 一句话两半对两半错的时候，
      该把它拆成两句，而不是留一句听起来对的。
    """
    h = hashlib.sha256()
    for p in sorted(recipe_dir.rglob("*")):
        if p.is_dir() or "__pycache__" in p.parts:
            continue
        rel = p.relative_to(recipe_dir).as_posix()
        if p.name == "spec.toml":
            _mix(h, rel, _toml_semantic(p))
        else:
            _mix(h, rel, sha256_of(p))
    return h.hexdigest()


def _toml_semantic(path: Path) -> str:
    """TOML 按语义哈希：解析出来、规范序列化。注释和排版都不参与。

    解析不了就退回原始字节，并且**说出来** —— 那意味着这一个文件会比它实际
    需要的变化得更频繁。（而 `skirt` 自己也会因为同一个原因报错，所以这条
    路正常走不到。）
    """
    src = path.read_text(encoding="utf-8")
    try:
        import tomllib
        data = tomllib.loads(src)
    except Exception as exc:                                    # noqa: BLE001
        print(f"  ⚠ build_key: {path.name} 解析不了（{type(exc).__name__}），"
              f"退回原始字节 —— 意味着改它的注释也会触发重建")
        return hashlib.sha256(src.encode("utf-8")).hexdigest()
    canon = json.dumps(data, sort_keys=True, ensure_ascii=False,
                       separators=(",", ":"), default=str)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


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


# ★ 哪些模块能改变一个包的【产物】。见 `tooling_hash` 里那一大段。
#
#   从 `build` / `pack` 出发沿 import 走出来的闭包（2026-10-20 实测）。
#   ★ `skirtlib` 之外还有 `skirt/skirt` 那个入口脚本 —— 它也算（改它可能
#     改变调用方式，那是产物的一部分）。
#
#   ★ 而这个集合【不是给人维护的清单】：`test_buildkey.py` 里有一条测试
#     从 `build` / `pack` 重算一遍 import 闭包，任何一个能走到而没列在这里的
#     模块都会让它红。
# ★★ 而其中有两个【推导不出来】，只能显式声明 —— 而它们的理由要写下来，
#   否则下一个人会以为清单是随手列的。
#
#   它们是【在调 `skirt build` 之前】由 `commands.py` 用到的：
#
#     deps    —— 算 sysroot 的合并顺序，以及 `SOURCE_DATE_EPOCH`
#                （后者直接决定产物里的时间戳 → 改它产物就变）
#     source  —— 下载并解开源码 → 决定"拿什么编"
#
#   而它们为什么不能从 `build` / `pack` 的 import 闭包里推出来：那条链是
#   `commands.cmd_build` → `deps.build_sysroot` / `source.fetch` → `build.执行`。
#   而 `commands.py` 里【还装着】restore / deploy / prune 那些和产物无关的东西，
#   所以不能拿它当种子（那会把它们全拉进来）。
#
#   ★ `test_buildkey.py` 里那条测试钉住的就是这个结构：
#       能推导的 ⊆ PAYLOAD_MODULES，而多出来的【恰好】是下面这两个。
PAYLOAD_MODULES_UNDERIVED = frozenset({"deps", "source"})

PAYLOAD_MODULES = frozenset({
    "build",      # 生成构建脚本（configure 参数、check_dest …）
    "pack",       # 包装成 .apk（mkpkg 参数、能力位、ELF 依赖）
    "spec",       # 读配方、展开占位符
    "deps",       # 依赖闭包、合 sysroot、SOURCE_DATE_EPOCH
    "source",     # 下载、校验 sha256、解压
    "elfdeps",    # 读 NEEDED / SONAME → 写进 provides/depends
    "common",     # 构建环境镜像名、日志、ROOT 这些
})


def tooling_hash() -> str:
    """工具集的内容 —— 驱动 + 打包脚本。注释和文档字符串剥掉之后。

    【为什么要进这个键】

    实测过，**只改驱动就能改变产物** —— 那次是给驱动加 `--libdir`，同一个 gcc
    的行为就变了。所以「同配方、同 gcc」不等于"同产物"。

    ★ 而 2026-10-15 之前这里**只哈希 `skirt/**/*.py`**，没算 `scripts/`。
      那是个真缺口：`scripts/pack.sh`（现在在 `skirtlib/pack.py`）决定
      「包里放什么」（它跑 `apk mkpkg`、
      恢复能力位、补 ELF 依赖），**改了它产物就变，而 key 不动** —— 正是
      "少一样比没有键更危险"那个形状。而它当场就被撞上了：那一天里 `pack.sh`
      改了两次（加安装脚本、换时间戳），两次都没进 key。

      所以现在连 `scripts/**/*.sh` 一起哈希。

    【两类的处理不一样，是有意的】

      · Python：`ast.parse` + 剥文档字符串 → 只改注释、只改格式都不进哈希
      · shell：原始字节 → **注释也算进去**

    为什么 shell 不剥：剥 shell 的注释要懂 heredoc、引号里的 `#`、续行 ——
    而**剥错的后果是漏掉一次真实改动**，那比多算几次严重得多。方向选保守那头。

    剥不掉或解析不了的文件一律退回原始字节，并且**说出来** —— 那意味着这一个
    文件会比它实际需要的变化得更频繁。
    """
    # ★ 而 `skirt/tests/` 要【排除】—— 2026-10-15 补的。
    #
    #   测试**不参与构建**，所以它们改了产物不会变。原先它们在 39 个文件里占
    #   22 个，于是"改一行测试"会让**每个包**的 build_key 都变 → 一次无谓的
    #   全量重建。
    #
    #   实测确认过：
    #       改一个测试文件里的【注释】   → key 不动（ast 剥掉了）
    #       改一个测试文件里的【代码行】 → key 变 ✗
    #   所以这不是"注释被剥了所以无所谓"—— 它是真的在算。
    #
    #   ★ 而上面那句「方向选保守那头」讲的是**一个文件内部**要剥多少（剥多了
    #     会漏掉真实改动）。这一条讲的是**哪些文件该进来**，是另一回事：
    #     测试不在产物那条因果链上，进来就是误报。
    # ★★★ 2026-10-20：从"哈希 skirt/ 下所有 .py"改成【只哈希能改变产物的那些】。
    #
    #   起因是一笔很实的账：那天我改了 `order.py` 三次（B1 那个修复的三个版本），
    #   而它【不在】任何产物的因果链上 —— 三次，每次触发一遍全量（2 小时）。
    #   同一类还有 `restore.py` / `repo.py` / `deploy.py`：改它们【不可能】改变
    #   任何 .apk 的内容。
    #
    #   ★ 而"哪些在因果链上"不该靠人记 —— 它是【算】出来的：从产物那条路
    #     （`build` / `pack`）出发，沿 import 走一遍。实测：
    #
    #         build · common · deps · elfdeps · pack · source · spec      7 个
    #
    #     而 order / restore / repo / deploy / commands / smoke / prune /
    #     collect / buildkey 一个都不在里面。
    #
    #   ★★ 而这个改动【方向是危险的】—— 2026-10-15 那次事故就是"哈希少了"
    #      （`scripts/pack.sh` 没算进去，于是改了它 key 不动、产物变了）。
    #      所以下面 `PAYLOAD_MODULES` 配一个测试：从 build/pack 出发算一遍
    #      import 闭包，**任何一个能走到而没列在这里的模块会让测试红**。
    #      那样"忘了加"就变成一次响亮的失败，而不是一次静默的旧产物。
    files = sorted(
        p for p in list((ROOT / "skirt" / "skirtlib").glob("*.py"))
        if p.stem in PAYLOAD_MODULES
    ) + sorted(
        p for p in list((ROOT / "scripts").rglob("*.sh"))
        if "__pycache__" not in p.parts
    )
    # ★ 而 `skirt/skirt`（那个 CLI 入口）【不在】里面 —— 它只决定命令怎么被
    #   调用，不改变任何 .apk 的内容。`__init__.py` 也不在（它是空的）。
    #
    #   实测的完整清单（2026-10-20）：
    #
    #       skirt/skirtlib/{build,common,deps,elfdeps,pack,source,spec}.py
    #       scripts/keys.sh
    #
    #   而它以前是 39 个文件（`skirt/**/*.py` 里除 tests 的全部）。
    h = hashlib.sha256()
    for p in files:
        src = p.read_text(encoding="utf-8")
        rel = p.relative_to(ROOT).as_posix()
        if p.suffix == ".py":
            try:
                norm = ast.unparse(_strip_docstrings(ast.parse(src)))
            except SyntaxError:
                norm = src
                print(f"  ⚠ build_key: {rel} 解析不了，用原始字节"
                      f"（意味着注释也算进去）")
        else:
            norm = src
        _mix(h, rel, hashlib.sha256(norm.encode("utf-8")).hexdigest())
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
        # ★ 名字从 `driver` 改成 `tooling`：它现在同时含驱动和打包脚本。
        #   而名字改对很重要 —— 原来是"driver"，会让人以为 scripts/ 已经在了。
        "tooling": tooling_hash(),
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
