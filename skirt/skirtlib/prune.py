"""把仓库里【过期的包】清掉。

    skirt prune [--arch x86_64] [--scope last-version|last-release] [--package NAME] [--go]

**默认干跑**：只打印会删什么、省多少字节。加 `--go` 才真删。

── 为什么要有它 ────────────────────────────────────────────────────────────

仓库只增不减地长胖了：2026-10-15 数了一遍，x86_64 有 118 条记录、93 个包名，
其中 14 个包名有多于一个版本；只留最新之后要删 25 个文件（21%）。而多版本的
那些【正是这一周里修过 bug 的那几个】：

    openssh          7 个：10.5_p1-r1 … r8（r1–r5 全是会覆盖 authorized_keys 的那个）
    filesystem       5 个
    yojigen-os-init  4 个

决策记录 `docs/决策记录/0020-软件包的清理策略.md` 把这件事定了：**manifest 是
记录，不是承诺** —— `-rX` 是我们的修订号，不承诺半年后还能按它重建。

── 三种范围（都要有）───────────────────────────────────────────────────────

    last-version   每个包名只留最高的那个 (版本, 修订号)      ← 默认，管日常
    last-release   同一个上游版本下只留最高的修订号
                   1.0-r1 / 1.0-r2 / 2.0-r1  →  留 1.0-r2 和 2.0-r1
    --package X    只对某一个包名套上面两种之一                ← 管应急

── "最新"由 apk 说了算 ─────────────────────────────────────────────────────

**不自己写版本比较函数。** apk 的版本比较有它自己的规则（`_pN`、字母后缀、
`-rN`），而写错一条的表现是【删掉了对的那个】——而那是不可逆的。

所以这里把要比较的版本对**一次问完**（`apk version -t A B` 打 `<` / `=` / `>`），
Python 只负责用那些判定挑最大值。实测过的几条（这几种都是真会碰到的）：

    1.0-r2      <  1.0-r10        ← 修订号是数字，不是字符串
    1.0_p1      <  1.0_p2
    1.0a        <  1.0b
    1.0         <  1.0-r1         ← 没有 -rN 的按 -r0 算
    1.0_p10     >  1.0_p9
    1.0_git…    >  1.0

── 顺序（不能反）──────────────────────────────────────────────────────────

    1. 先删文件
    2. 再用【剩下的集合】重建索引
    3. 最后传索引

反过来的话，索引里会留着一批已经不在的文件 —— `apk add` 到它们会 404，而
"索引在、包不在"这件事人看不出区别。

★ 重建索引那一步直接复用 `repo.build_index()`（不抄一份）：它建完还会拿
  `apk adbdump` 把索引和仓库里的包对一遍。

── 它不做什么 ─────────────────────────────────────────────────────────────

 · 不动"上传"本身：日常发布照旧是 `rclone copy`（只增）。清理是一个单独的、
   显式的动作，不是发布流程的副作用。
 · 不碰别的架构、不碰别的仓库：只动 `<架构>/` 下的包和那一个索引。
 · **索引里没有的包不删**：只报告。删一个"说不出它是什么"的文件，那是另一个
   决定，而这个脚本不做。
"""

from __future__ import annotations

import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .common import die
from .repo import (INDEX_NAME, S3_VARS, filename_for, parse_index_description,
                   parse_index_dump, parse_pkgname_spec, rclone, remote_path)

#: 三种范围里的两种（第三种是"只对一个包"的修饰）
SCOPES = ("last-version", "last-release")

#: 版本结尾的修订号：`10.5_p1-r8` → 上游是 `10.5_p1`、修订号是 8
_RELEASE_RE = re.compile(r"^(?P<up>.*?)-r(?P<rel>[0-9]+)$")


# ------------------------------------------------------------------ 数据

@dataclass(frozen=True)
class Pkg:
    """线上一个包。"""

    filename: str          #: `<名字>-<版本>.apk`（pkgname-spec 就是这么拼的）
    size: int              #: 字节
    name: str              #: 包名
    version: str           #: apk 说的版本（含 `-rN`）


@dataclass
class Plan:
    """清理计划。"""

    keep: list[Pkg] = field(default_factory=list)
    drop: list[Pkg] = field(default_factory=list)
    unknown: list[tuple[str, int]] = field(default_factory=list)   #: (文件名, 字节)
    scope: str = SCOPES[0]
    package: str | None = None

    @property
    def drop_bytes(self) -> int:
        return sum(p.size for p in self.drop)

    @property
    def total_bytes(self) -> int:
        return self.drop_bytes + sum(p.size for p in self.keep)


def upstream_of(version: str) -> str:
    """哪个上游版本 —— 去掉结尾那个 `-r<数字>`。

    没有 `-rN` 的原样返回（apk 把"没有"当 `-r0` 看，实测 `1.0 < 1.0-r1`）。
    """
    m = _RELEASE_RE.match(version)
    return m.group("up") if m else version


def group_key(pkg: Pkg, scope: str) -> tuple:
    """同一个组里才比"最新"。

    `last-version` 按包名分组（每个包名只有一个赢家）；
    `last-release` 按 (包名, 上游版本) 分组（上游版本换了就各留各的）。
    """
    if scope == "last-release":
        return (pkg.name, upstream_of(pkg.version))
    return (pkg.name,)


def group_pkgs(pkgs: list[Pkg], scope: str, package: str | None = None
               ) -> dict[tuple, list[Pkg]]:
    """分组 —— 只留"要算"的那批（`--package` 之外的不进组，等于不碰）。"""
    groups: dict[tuple, list[Pkg]] = {}
    for pkg in pkgs:
        if package is not None and pkg.name != package:
            continue
        groups.setdefault(group_key(pkg, scope), []).append(pkg)
    return groups


def needed_pairs(pkgs: list[Pkg], scope: str, package: str | None = None
                 ) -> list[tuple[str, str]]:
    """要问 apk 的那些版本对（组内两两比）。

    组都很小（最多的 openssh 是 7 个 → 21 对），所以一次问完不贵，而
    **不用在 Python 里排序** —— 排错的后果和比错一样。
    """
    pairs: list[tuple[str, str]] = []
    for group in group_pkgs(pkgs, scope, package).values():
        for i, a in enumerate(group):
            for b in group[i + 1:]:
                pairs.append((a.version, b.version))
    return pairs


def plan(pkgs: list[Pkg], *, scope: str = SCOPES[0], package: str | None = None,
         newer) -> Plan:
    """算计划。`newer(a, b)` 回答"a 比 b 新吗"（由 apk 的判定喂进来）。

    ★ 拿不准就**不删**：`newer` 对某一对说不出来的时候必须抛（见
      `plan_with_apk`）——"不知道谁新"的时候删掉一个，那是最坏的结果。
    """
    if scope not in SCOPES:
        die(f"不认识的 --scope: {scope}（只有 {' / '.join(SCOPES)}）")

    keep, drop = [], []
    for key, group in sorted(group_pkgs(pkgs, scope, package).items()):
        best = group[0]
        for pkg in group[1:]:
            if newer(pkg.version, best.version):
                best = pkg
        for pkg in group:
            (keep if pkg is best else drop).append(pkg)

    return Plan(keep=keep, drop=drop,
                unknown=[],           # 由调用方填（线上有、索引里没有的那些）
                scope=scope, package=package)


# ------------------------------------------------------------------ 问 apk

def apk_compare(pairs: list[tuple[str, str]], image: str = "alpine:latest"
                ) -> dict[tuple[str, str], str]:
    """一批比较，一次问完。返回 {(a, b): "<" | "=" | ">"}。

    `apk version -t A B` 打的就是这三种之一。
    """
    if not pairs:
        return {}

    out: dict[tuple[str, str], str] = {}
    with tempfile.TemporaryDirectory(prefix="skirt-prune-") as td:
        work = Path(td)
        (work / "pairs").write_text(
            "".join(f"{a}\t{b}\n" for a, b in pairs), encoding="utf-8")
        # ★ `< /dev/null` 不是多余的：while read 的循环体里谁读了 stdin，
        #   剩下的清单就被它吃掉了。
        (work / "cmp.sh").write_text(
            # ★ IFS 里那个必须是【真的制表符】：写成 '\\t' 的话 shell 看到的是
            #   反斜杠+t 两个字符，于是整行都算第一个字段、第二个是空的
            #   （`apk version -t "1.0-r1" ""` 什么都不输出 —— 而症状是"没有
            #   一对有答案"，离成因很远）。
            "while IFS='\t' read -r a b; do\n"
            "    [ -n \"$a\" ] || continue\n"
            "    printf '@@cmp\\t%s\\t%s\\t%s\\n' \"$a\" \"$b\""
            " \"$(apk version -t \"$a\" \"$b\" </dev/null)\"\n"
            "done < /work/pairs\n", encoding="utf-8")
        r = run_in_container(["sh", "/work/cmp.sh"], work, image)

    for line in r.stdout.splitlines():
        if line.startswith("@@cmp\t"):
            _, a, b, verdict = line.split("\t")
            out[(a, b)] = verdict
    missing = [p for p in pairs if p not in out]
    if missing:
        die(f"apk version -t 有 {len(missing)} 对没给答案"
            f"（例如 {missing[0]}）——那就不敢删，先看清楚")
    return out


def run_in_container(argv: list[str], work: Path, image: str = "alpine:latest"):
    """跑一个【临时容器】，把 work 挂成 /work（只读）。

    （和 repo.rclone 的区别：那个跑的是 rclone，这个跑的是 apk。）
    """
    cmd = ["docker", "run", "--rm", "-v", f"{work}:/work:ro", image, *argv]
    try:
        return subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        die("没有 docker —— 版本比较要问容器里的 apk version")


def plan_with_apk(pkgs: list[Pkg], *, scope: str = SCOPES[0],
                  package: str | None = None,
                  compare=apk_compare) -> Plan:
    """`plan()` + 真的问 apk。"""
    verdicts = compare(needed_pairs(pkgs, scope, package))

    def newer(a: str, b: str) -> bool:
        if a == b:
            return False
        v = verdicts.get((a, b))
        if v is None and (b, a) in verdicts:
            # 只问了一个方向（问哪些对由分组算出来，而挑最大值的顺序不由它定）。
            # 反着用是安全的：apk 的版本比较是全序 —— `<` 反过来就是 `>`
            # （实测那几条都是这样）。
            v = {"<": ">", ">": "<", "=": "="}[verdicts[(b, a)]]
        if v is None:
            die(f"没有 {a} 和 {b} 的比较结果 —— 不敢猜，先看清楚")
        return v == ">"

    return plan(pkgs, scope=scope, package=package, newer=newer)


# ------------------------------------------------------------------ 线上那份

@dataclass
class Remote:
    """线上仓库在哪。

    线上是 S3（凭据在环境变量里）；测试拿一个本地目录当远端
    （rclone 的 `:local:` + 一个挂载），就不需要真凭据。
    """

    path: str                              #: rclone 的 remote:path
    env: dict = field(default_factory=dict)
    mounts: tuple = ()                     #: ((宿主路径, 容器里路径), …)

    @classmethod
    def from_env(cls, arch: str, env: dict | None = None) -> "Remote":
        import os

        env = os.environ if env is None else env
        missing = [k for k in S3_VARS if not env.get(k)]
        if missing:
            die("没有 S3 凭据（" + " / ".join(missing) + "）——列线上要它。\n"
                "       本机没有是正常的（它们只在 CI 的 secret 里）；"
                "本地想试就用一个本地目录当远端（见 test_prune.py）。")
        return cls(path=remote_path(arch, env), env=env)

    def icon(self) -> str:
        return f"{self.path}/{INDEX_NAME}"


def list_remote(remote: Remote, image: str = "rclone/rclone") -> dict[str, int]:
    """线上有什么：{文件名: 字节}。

    ★ 用 `lsf --files-only --format sp`（`size;path`）。**不用 `ls`**：那是给
      人看的，带日期和尺寸的表格，还按目录分组。
    """
    r = rclone(["lsf", "--files-only", "--format", "sp", remote.path],
               remote.env, mounts=list(remote.mounts), image=image)
    if r.returncode != 0:
        print(r.stderr.strip())
        die(f"列不了线上（rclone 退出码 {r.returncode}）")
    out: dict[str, int] = {}
    for line in r.stdout.splitlines():
        if not line.strip():
            continue
        size, _, name = line.partition(";")
        if not name:
            continue
        out[name] = int(size) if size.isdigit() else 0
    return out


def fetch_index(remote: Remote, dest_dir: Path, image: str = "rclone/rclone") -> Path:
    """把线上的索引拉下来（要读它才知道每个文件的 (名字, 版本)）。"""
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / INDEX_NAME
    dest.unlink(missing_ok=True)
    r = rclone(["copyto", remote.icon(), f"/repo/{INDEX_NAME}"],
               remote.env, mounts=list(remote.mounts) + [(dest_dir, "/repo")],
               image=image)
    if r.returncode != 0 or not dest.is_file():
        print(r.stderr.strip())
        die(f"拉不到线上的索引（{remote.icon()}）——先跑一次 `skirt repo` 吗？")
    return dest


def delete_remote(remote: Remote, names: list[str], image: str = "rclone/rclone") -> None:
    """线上删掉这些文件（一份清单，一次删完）。"""
    if not names:
        return
    with tempfile.TemporaryDirectory(prefix="skirt-prune-") as td:
        work = Path(td)
        (work / "files").write_text("".join(f"{n}\n" for n in names), encoding="utf-8")
        r = rclone(["delete", "--files-from", "/work/files", remote.path],
                   remote.env,
                   mounts=list(remote.mounts) + [(work, "/work")], image=image)
    if r.returncode != 0:
        print(r.stderr.strip())
        die(f"删不动（rclone 退出码 {r.returncode}）——一个都没删干净之前先别看别处")


def upload_index(remote: Remote, index: Path, image: str = "rclone/rclone") -> None:
    """把新索引传上去（就这一个文件）。"""
    r = rclone(["copyto", f"/repo/{index.name}", remote.icon()],
               remote.env, mounts=list(remote.mounts) + [(index.parent, "/repo")],
               image=image)
    if r.returncode != 0:
        print(r.stderr.strip())
        die(f"索引没传上去（rclone 退出码 {r.returncode}）")


# ------------------------------------------------------------------ 拼

def index_filenames(dump: str, arch: str) -> dict[str, tuple[str, str]]:
    """索引渲染 → {<架构目录里的文件名>: (名字, 版本)}。

    ★ 文件名不是猜的：索引里存着 `pkgname-spec`（下载路径的模板），把
      `${name}`/`${version}` 换掉就是 mkndx 那边实际用的名字。没有那一行
      （老索引？）才退回默认的 `名字-版本.apk`，而**那件事要说出来**。

    ★ 而那个模板是【相对于仓库根】的（`x86_64/${name}-${version}.apk`），
      我们列的是 `<仓库根>/<架构>/` 里面 —— 所以要把架构那一层剥掉。
      不剥的后果不是报错，而是**一个都对不上**：所有条目都变成"线上没有这个
      文件"，计划空的，于是 prunce 一言不发什么都不删。安静得看不出来。
    """
    spec = parse_pkgname_spec(dump)
    if not spec:
        print("  ★ 索引里没有 pkgname-spec —— 按默认的 <名字>-<版本>.apk 对文件名")
        spec = "${name}-${version}.apk"
    prefix = f"{arch}/"
    if not spec.startswith(prefix):
        print(f"  ★ pkgname-spec（{spec}）不以 `{prefix}` 开头 —— "
              f"按原样对文件名，对不上就会全部报成'线上没有'")
    out: dict[str, tuple[str, str]] = {}
    for name, version in parse_index_dump(dump):
        path = filename_for(spec, name, version)
        out[path[len(prefix):] if path.startswith(prefix) else path] = (name, version)
    return out


def collect(sizes: dict[str, int], dump: str, arch: str
            ) -> tuple[list[Pkg], list[tuple[str, int]], list[str]]:
    """(线上有什么 + 索引说了什么) → (要算的包, 索引里说不出来的文件, 索引里列了而线上没有的)。

    ★ 索引里没有的文件【不删】——只报告。删一个"说不出它是什么"的文件是另一个
      决定，这个脚本不做。
    """
    by_file = index_filenames(dump, arch)
    pkgs: list[Pkg] = []
    missing: list[str] = []
    for filename, (name, version) in sorted(by_file.items()):
        if filename not in sizes:
            missing.append(filename)
            continue
        pkgs.append(Pkg(filename=filename, size=sizes[filename],
                        name=name, version=version))
    known = set(by_file)
    unknown = [(f, s) for f, s in sorted(sizes.items())
               if f.endswith(".apk") and f not in known]
    return pkgs, unknown, missing


def describe(plan: Plan, arch: str) -> None:
    """把计划打出来（干跑就是靠这一段）。"""
    scope_text = {"last-version": "每个包名只留最新的那个",
                  "last-release": "同一个上游版本下只留最新那个修订"}[plan.scope]
    what = f"包名 {plan.package}" if plan.package else "全部包"
    print(f"  范围: {scope_text} · 只看 {what}")
    print(f"  线上 {len(plan.keep) + len(plan.drop)} 个包"
          f"（{plan.total_bytes / 1048576:.1f} MiB）")
    if not plan.drop:
        print("  没有要删的 —— 每个包名都已经只剩最新的了")
        return
    winners = {group_key(p, plan.scope): p.version for p in plan.keep}
    for pkg in sorted(plan.drop, key=lambda p: (p.name, p.version)):
        winner = winners.get(group_key(pkg, plan.scope), "（同组里最新的那个）")
        print(f"    - {pkg.filename:<40} {pkg.size / 1048576:7.2f} MiB"
              f"   → 留 {winner}")
    total = plan.total_bytes or 1
    print(f"  会删 {len(plan.drop)} 个包 · "
          f"{plan.drop_bytes / 1048576:.1f} MiB（占 {plan.drop_bytes / total:.0%}）·"
          f" 留 {len(plan.keep)} 个")
    if plan.unknown:
        print(f"  ★ 另外 {len(plan.unknown)} 个文件在线上、而索引里没有它们，"
              f"【没碰】：")
        for name, size in plan.unknown[:5]:
            print(f"      {name}  {size} 字节")


# ------------------------------------------------------------------ 总入口

def run(*, arch: str, remote: Remote, scope: str = SCOPES[0],
        package: str | None = None, go: bool = False,
        sign_key: Path | None = None, desc: str | None = None,
        repo_dir: Path | None = None, root: Path | None = None,
        rclone_image: str = "rclone/rclone",
        apk_image: str = "alpine:latest") -> Plan:
    """干跑（默认）或真删。返回计划。打印都在这里 —— 干跑的输出就是它的产物。

    ★ 真删那一段的顺序是死的（决策记录 0020）：

        拉全 → 删文件（线上 + 本地这份）→ 用剩下的重建索引 → 传索引

      反过来索引里会留着已经不在的文件。
    """
    from . import repo as repo_mod
    from .common import REPO_DIR, ROOT

    repo_dir = REPO_DIR / arch if repo_dir is None else repo_dir
    root = ROOT if root is None else root

    print()
    print(f"  线上: {remote.path}")
    sizes = list_remote(remote, rclone_image)
    fetch_index(remote, repo_dir, rclone_image)
    dump = repo_mod.dump_index(repo_dir, apk_image)

    pkgs, unknown, missing = collect(sizes, dump, arch)
    if package is not None and not any(p.name == package for p in pkgs):
        die(f"线上没有这个包名: {package}")

    planned = plan_with_apk(pkgs, scope=scope, package=package,
                            compare=lambda pairs: apk_compare(pairs, apk_image))
    planned.unknown = unknown

    print()
    describe(planned, arch)
    if missing:
        print(f"  ★ 索引里列了 {len(missing)} 个包、而线上没有它们的文件"
              f"（没碰）：{', '.join(missing[:3])}"
              + ("…" if len(missing) > 3 else ""))

    if not go:
        print()
        print("  （这是干跑。要真删：加 --go）")
        return planned

    if not planned.drop:
        print()
        print("  没有要删的 —— 索引不用重建，也不传")
        return planned

    names = [p.filename for p in planned.drop]
    print()
    print("  拉回线上已有的包（重建索引要【全套】在本地）…")
    pulled = repo_mod.pull_apks(remote.path, repo_dir, remote.env,
                                 rclone_image, mounts=list(remote.mounts))
    if pulled.returncode != 0:
        print(pulled.stderr.strip())
        die(f"拉不全（rclone 退出码 {pulled.returncode}）——"
            f"没有全套就没法重建索引，一个文件都别删")
    print(f"  ① 删 {len(names)} 个文件（线上 + var/repo 里那份）")
    delete_remote(remote, names, rclone_image)
    for name in names:
        (repo_dir / name).unlink(missing_ok=True)

    print(f"  ② 用剩下的 {len(planned.keep)} 个包重建索引"
          f"（就是 `skirt repo` 那步，复用它的实现）")
    if desc:
        print(f"     描述: {desc}（命令行给的）")
    else:
        desc = parse_index_description(dump) or f"Yojigen OS {arch} 软件仓库"
        print(f"     描述: {desc}"
              + ("" if parse_index_description(dump) else "（★ 索引里原来那份读不到，用默认的）"))
    index = repo_mod.build_index(arch=arch, pkgs_dir=repo_dir, repo_dir=repo_dir,
                                 desc=desc, sign_key=sign_key, sync=False,
                                 image=apk_image, root=root)

    print("  ③ 传索引（就这一个文件）")
    upload_index(remote, index, rclone_image)
    print()
    print(f"  ✓ 删了 {len(names)} 个 · 索引里现在有 {len(planned.keep)} 个包")
    return planned
