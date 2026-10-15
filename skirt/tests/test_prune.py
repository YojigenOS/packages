#!/usr/bin/env python3
"""prune 的测试 —— 重点是"该删哪些"那部分。

【为什么这一组要写得厚】

清理是**不可逆**的：删错一个包，客户端那边就是 404，而你没法把文件变回来
（要重建那个版本的包，而那个版本可能正是有问题的那个）。所以"该删哪些"这条
判定要有好几条测试守着 —— 三种范围各一条，加上那几种真的会碰到的版本形状：

    上游换了版本      1.0-r1 / 1.0-r2 / 2.0-r1
    修订号跳号        filesystem 线上就是 r1 r2 r4 r5 r6（r3 不在了）
    只有一个版本      不动它
    `_pN` / 字母后缀  1.0_p1 < 1.0_p2 · 1.0a < 1.0b
    没有 `-rN`        1.0 < 1.0-r1（apk 把"没有"当 -r0）

★ 而"谁更新"这件事**不在这里算**：`plan()` 要一个 `newer` 比较器，由 apk 喂
  进来（`apk version -t`）。所以这一组里两种测试都要有：

    · 用【假的比较器】测判定逻辑（不用 docker，几十毫秒）
    · 用【真的 apk】测比较结果（要 docker，见 Test真的apk版本比较）

  假比较器按 apk 的规则实现那几条形状，而真 apk 的判定在 docker 那条里
  逐条钉住 —— 两边对上，才说明"逻辑"和"规则"都没有各说各话。

不用 docker 的都在下面；要 docker 的两组标了 skip。
"""

from __future__ import annotations

import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from skirtlib import prune, repo
from skirtlib.prune import Pkg


def _docker_ready() -> bool:
    if shutil.which("docker") is None:
        return False
    r = subprocess.run(["docker", "image", "inspect", "alpine:latest"],
                       capture_output=True)
    return r.returncode == 0


def _rclone_ready() -> bool:
    if shutil.which("docker") is None:
        return False
    r = subprocess.run(["docker", "image", "inspect", "rclone/rclone"],
                       capture_output=True)
    return r.returncode == 0


# ------------------------------------------------------------------ 假的"谁更新"

#: apk 版本比较的那几条规则（实测过，见 Test真的apk版本比较）。
#: 这是个【测试用】的实现，只为了在不用 docker 的地方也能测判定逻辑 ——
#: 它不冒充 apk：真判定由容器里的 apk version -t 给。
def _fake_newer(a: str, b: str) -> bool:
    def parts(v: str):
        # 上游版本 + 修订号：`1.0_p2-r3` → ("1.0_p2", 3)
        m = re.match(r"^(?P<up>.*?)(?:-r(?P<rel>[0-9]+))?$", v)
        up, rel = m.group("up"), int(m.group("rel") or 0)

        def key(text: str):
            out = []
            for piece in re.split(r"[._]", text):
                m2 = re.match(r"^([0-9]*)([a-z]*)([0-9]*)$", piece)
                if m2:
                    out.append((int(m2.group(1) or 0), m2.group(2),
                                int(m2.group(3) or 0)))
                else:
                    out.append((0, piece, 0))
            return out

        return key(up), rel

    return parts(a) > parts(b)


def _pkgs(entries: list[tuple[str, str]]) -> list[Pkg]:
    """(名字, 版本) → Pkg 列表（文件名按 pkgname-spec 那个模板拼）。"""
    return [Pkg(filename=f"{n}-{v}.apk", size=1000, name=n, version=v)
            for n, v in entries]


# ------------------------------------------------------------------ 三种范围

class Test该删哪些(unittest.TestCase):
    def test_默认_每个包名只留最新那个(self):
        pkgs = _pkgs([("openssh", f"10.5_p1-r{i}") for i in range(1, 9)]
                     + [("zlib", "1.3.2-r1")])

        plan = prune.plan(pkgs, newer=_fake_newer)

        self.assertEqual([p.version for p in plan.keep],
                         ["10.5_p1-r8", "1.3.2-r1"])
        self.assertEqual(len(plan.drop), 7)
        self.assertEqual({p.version for p in plan.drop},
                         {f"10.5_p1-r{i}" for i in range(1, 8)})

    def test_last_release_上游版本各留一个修订(self):
        """1.0-r1 / 1.0-r2 / 2.0-r1 → 留 1.0-r2 和 2.0-r1。"""
        pkgs = _pkgs([("hello", "1.0-r1"), ("hello", "1.0-r2"),
                      ("hello", "2.0-r1")])

        plan = prune.plan(pkgs, scope="last-release", newer=_fake_newer)

        self.assertEqual(sorted(p.version for p in plan.keep),
                         ["1.0-r2", "2.0-r1"])
        self.assertEqual([p.version for p in plan.drop], ["1.0-r1"])

    def test_last_version_会把上游老版本也删掉(self):
        """默认那条range 更狠：只留一个包名的最新，所以 1.0-r2 也要走。"""
        pkgs = _pkgs([("hello", "1.0-r1"), ("hello", "1.0-r2"),
                      ("hello", "2.0-r1")])

        plan = prune.plan(pkgs, newer=_fake_newer)

        self.assertEqual([p.version for p in plan.keep], ["2.0-r1"])
        self.assertEqual(sorted(p.version for p in plan.drop), ["1.0-r1", "1.0-r2"])

    def test_只清一个包_别的名字一个都不碰(self):
        pkgs = _pkgs([("openssh", "10.5_p1-r1"), ("openssh", "10.5_p1-r8"),
                      ("zlib", "1.3.2-r1"), ("zlib", "1.3.2-r2")])

        plan = prune.plan(pkgs, package="openssh", newer=_fake_newer)

        self.assertEqual([p.filename for p in plan.drop], ["openssh-10.5_p1-r1.apk"])
        # zlib 那两个【不在计划里】=> 不会被删（要删只删 plan.drop）
        self.assertEqual({p.name for p in plan.keep + plan.drop}, {"openssh"})

    def test_指定一个线上没有的包名_什么都不删(self):
        """命令层会为这个报错（`run()` 里那条），这里只钉住"计划是空的"。"""
        pkgs = _pkgs([("zlib", "1.3.2-r1")])

        plan = prune.plan(pkgs, package="查无此包", newer=_fake_newer)

        self.assertEqual(plan.drop, [])
        self.assertEqual(plan.keep, [])

    def test_只有一个版本时不动它(self):
        pkgs = _pkgs([("zlib", "1.3.2-r1")])

        plan = prune.plan(pkgs, newer=_fake_newer)

        self.assertEqual([p.filename for p in plan.keep], ["zlib-1.3.2-r1.apk"])
        self.assertEqual(plan.drop, [])

    def test_修订号跳号不管它(self):
        """线上 filesystem 就是 r1 r2 r4 r5 r6 —— r3 不在了。

        ★ 它【不该】被当成"缺了一环"去处理：我们的判据是"谁最高"，而不是
          "编号连不连续"。拿编号去推"应该有几个"，就会把跳号当成异常。
        """
        pkgs = _pkgs([("filesystem", v) for v in
                      ("1.0-r1", "1.0-r2", "1.0-r4", "1.0-r5", "1.0-r6")])

        plan = prune.plan(pkgs, newer=_fake_newer)

        self.assertEqual([p.version for p in plan.keep], ["1.0-r6"])
        self.assertEqual(len(plan.drop), 4)

    def test_修订号按数字比_不是按字符串(self):
        """`1.0-r10` 比 `1.0-r9` 新 —— 字符串比会得出反的结论，然后删错。"""
        pkgs = _pkgs([("hello", "1.0-r2"), ("hello", "1.0-r10")])

        plan = prune.plan(pkgs, newer=_fake_newer)

        self.assertEqual([p.version for p in plan.keep], ["1.0-r10"])

    def test_没有_rN_的按_r0_算(self):
        pkgs = _pkgs([("hello", "1.0"), ("hello", "1.0-r1")])

        plan = prune.plan(pkgs, newer=_fake_newer)

        self.assertEqual([p.version for p in plan.keep], ["1.0-r1"])

    def test_字母后缀(self):
        pkgs = _pkgs([("hello", "1.0a"), ("hello", "1.0b")])

        plan = prune.plan(pkgs, newer=_fake_newer)

        self.assertEqual([p.version for p in plan.keep], ["1.0b"])

    def test_pN_的后缀(self):
        pkgs = _pkgs([("openssh", "10.5_p1-r8"), ("openssh", "10.5_p2-r1")])

        plan = prune.plan(pkgs, newer=_fake_newer)

        self.assertEqual([p.version for p in plan.keep], ["10.5_p2-r1"])

    def test_不认识的_scope_要报错(self):
        with self.assertRaises(SystemExit):
            prune.plan(_pkgs([("hello", "1.0-r1")]), scope="随便", newer=_fake_newer)

    def test_拿不准就不删(self):
        """比较器对某一对说不出来 → plan_with_apk 必须拦下（不猜）。"""
        def 半吊子(pairs):
            return {}                      # 一对都不给答案

        with self.assertRaises(SystemExit):
            prune.plan_with_apk(_pkgs([("hello", "1.0-r1"), ("hello", "1.0-r2")]),
                                compare=半吊子)

    def test_要问的比较对是组内的两两(self):
        pkgs = _pkgs([("a", "1.0-r1"), ("a", "1.0-r2"), ("b", "2.0-r1")])

        pairs = prune.needed_pairs(pkgs, "last-version")

        self.assertEqual(pairs, [("1.0-r1", "1.0-r2")])   # b 只有一个，不用比


# ------------------------------------------------------------------ 索引 ↔ 文件

#: 索引渲染（带 pkgname-spec 那一行 —— 真的索引里就有）
_INDEX_DUMP = """\
#%SCHEMA: 78646E69
# ADB block, size: 356, compat: 0, ver: 0
description: Yojigen OS x86_64 软件仓库
packages: # 3 items
  - name: openssh
    version: 10.5_p1-r8
    arch: x86_64
  - name: openssh
    version: 10.5_p1-r7
    arch: x86_64
  - name: zlib
    version: 1.3.2-r1
    arch: x86_64
pkgname-spec: x86_64/${name}-${version}.apk
"""


class Test索引对到文件名(unittest.TestCase):
    def test_文件名从_pkgname_spec_算_并剥掉架构那一层(self):
        """★ spec 是相对【仓库根】的（`x86_64/…`），而我们列的是架构目录【里面】。

        不剥掉那一层的话，一个都对不上 —— 而症状是"什么都没删"（沉默的错）。
        """
        got = prune.index_filenames(_INDEX_DUMP, "x86_64")

        self.assertEqual(got, {
            "openssh-10.5_p1-r8.apk": ("openssh", "10.5_p1-r8"),
            "openssh-10.5_p1-r7.apk": ("openssh", "10.5_p1-r7"),
            "zlib-1.3.2-r1.apk": ("zlib", "1.3.2-r1"),
        })

    def test_没有_pkgname_spec_就退回默认_并说出来(self):
        dump = _INDEX_DUMP.replace("pkgname-spec: x86_64/${name}-${version}.apk\n", "")

        buf = io.StringIO()
        with redirect_stdout(buf):
            got = prune.index_filenames(dump, "x86_64")

        self.assertIn("zlib-1.3.2-r1.apk", got)
        self.assertIn("pkgname-spec", buf.getvalue())

    def test_线上有而索引里没有的文件_不删_只报告(self):
        sizes = {"openssh-10.5_p1-r8.apk": 100,
                 "openssh-10.5_p1-r7.apk": 90,
                 "zlib-1.3.2-r1.apk": 50,
                 "谁知道这是什么-1.0-r1.apk": 7,
                 "APKINDEX.tar.gz": 999}

        pkgs, unknown, missing = prune.collect(sizes, _INDEX_DUMP, "x86_64")

        self.assertEqual(len(pkgs), 3)
        self.assertEqual(missing, [])
        self.assertEqual([f for f, _ in unknown], ["谁知道这是什么-1.0-r1.apk"])

        plan = prune.plan(pkgs, newer=_fake_newer)
        self.assertNotIn("谁知道这是什么-1.0-r1.apk",
                         [p.filename for p in plan.drop])

    def test_索引里列了而线上没有的文件_也要报告(self):
        sizes = {"zlib-1.3.2-r1.apk": 50}

        pkgs, unknown, missing = prune.collect(sizes, _INDEX_DUMP, "x86_64")

        self.assertEqual([p.filename for p in pkgs], ["zlib-1.3.2-r1.apk"])
        self.assertEqual(sorted(missing),
                         ["openssh-10.5_p1-r7.apk", "openssh-10.5_p1-r8.apk"])
        self.assertEqual(unknown, [])


class Test干跑的输出(unittest.TestCase):
    def test_打印会删什么_留谁_省多少(self):
        pkgs = _pkgs([("openssh", "10.5_p1-r1"), ("openssh", "10.5_p1-r8")])
        for p in pkgs:
            object.__setattr__(p, "size", 1024 * 1024)

        plan = prune.plan(pkgs, newer=_fake_newer)
        buf = io.StringIO()
        with redirect_stdout(buf):
            prune.describe(plan, "x86_64")

        out = buf.getvalue()
        self.assertIn("openssh-10.5_p1-r1.apk", out)      # 会删哪个
        self.assertIn("留 10.5_p1-r8", out)               # 留谁
        self.assertIn("会删 1 个包 · 1.0 MiB", out)        # 多少
        self.assertIn("占 50%", out)

    def test_没有要删的就说清楚(self):
        plan = prune.plan(_pkgs([("zlib", "1.3.2-r1")]), newer=_fake_newer)

        buf = io.StringIO()
        with redirect_stdout(buf):
            prune.describe(plan, "x86_64")

        self.assertIn("没有要删的", buf.getvalue())


# ------------------------------------------------------------------ 真的 apk（要 docker）

@unittest.skipUnless(_docker_ready(), "需要 docker 和 alpine 镜像")
class Test真的apk版本比较(unittest.TestCase):
    """★ 这几条是 2026-10-15 实测过的，逐条钉在这里。

    它们说明"最新"这件事【不是我们定义的】：`apk version -t` 说什么就是什么。
    哪天 apk 换了规则，这一组会先响 —— 而不是我们先删错了包。
    """

    CASES = [
        (("1.0-r2", "1.0-r10"), "<"),        # 修订号是数字
        (("1.0_p1", "1.0_p2"), "<"),         # _pN 也是
        (("1.0_p10", "1.0_p9"), ">"),
        (("1.0a", "1.0b"), "<"),             # 字母后缀
        (("1.0", "1.0-r1"), "<"),            # 没有 -rN = -r0
        (("1.0_git20261015", "1.0"), ">"),
        (("10.5_p1-r1", "10.5_p1-r8"), "<"),   # openssh 那条真的
        (("2.0", "1.9-r9"), ">"),
    ]

    def test_逐条对上(self):
        verdicts = prune.apk_compare([pair for pair, _ in self.CASES])

        for pair, want in self.CASES:
            self.assertEqual(verdicts[pair], want, f"{pair[0]} vs {pair[1]}")

    def test_一次问完_顺序无关(self):
        pair = ("1.0-r2", "1.0-r10")

        self.assertEqual(prune.apk_compare([pair]), {pair: "<"})
        self.assertEqual(prune.apk_compare([pair, pair])[pair], "<")


# ------------------------------------------------------------------ 端到端（要 docker + rclone）

def _make_pkg(tmp: Path, out_dir: Path, name: str, version: str) -> None:
    src = tmp / f"src-{name}-{version}"
    (src / "usr/bin").mkdir(parents=True, exist_ok=True)
    (src / "usr/bin" / name).write_text(f"#!/bin/sh\necho {name} {version}\n")
    script = (f"apk mkpkg --info name:{name} --info version:{version} "
              f"--info arch:x86_64 --files /w/{src.relative_to(tmp)} "
              f"--output /w/{out_dir.relative_to(tmp)}/{name}-{version}.apk || exit 1\n"
              f"chown -R {os.getuid()}:{os.getgid()} /w\n")
    r = subprocess.run(["docker", "run", "--rm", "--entrypoint", "sh",
                        "-v", f"{tmp}:/w", "alpine:latest", "-c", script],
                       capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, f"造包失败：\n{r.stdout}{r.stderr}"


@unittest.skipUnless(_docker_ready() and _rclone_ready(), "需要 docker + alpine + rclone 镜像")
class Test端到端_本地目录当远端(unittest.TestCase):
    """把线上那套（列包 → 读索引 → 算 → 删 → 重建索引 → 传索引）跑一遍。

    远端换成**一个本地目录**（rclone 的 `:local:` + 一个挂载），所以不需要
    S3 凭据 —— 而 `:local:` 和 `yojigen:` 走的是同一个 rclone、同一批参数，
    所以"rclone 那条路"本身是被真跑过的。
    """

    TIMEOUT = 600

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="prune-e2e-")
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.data = self.tmp / "data"
        self.remote_dir = self.data / "x86_64"
        self.remote_dir.mkdir(parents=True)
        self.staging = self.tmp / "repo/x86_64"    # 本地那份（= var/repo/<架构>）
        self.staging.mkdir(parents=True)
        self.root = self.tmp / "root"          # 签名记号写这儿，别碰真仓库

    def _remote(self) -> prune.Remote:
        return prune.Remote(path=":local:/data/x86_64", env={},
                            mounts=((self.data, "/data"),))

    def _seed_remote(self, entries: list[tuple[str, str]]) -> None:
        """把"线上那个目录"造出来：几个真包 + 一份真的索引。"""
        for name, version in entries:
            _make_pkg(self.tmp, self.remote_dir, name, version)
        # 线上那份索引：用 skirt repo 自己的实现建（同一个东西，别抄一份）
        # ★ pkgs_dir 和 repo_dir 都指线上那个目录 —— 索引是从【这些包】建的
        repo.build_index(arch="x86_64", pkgs_dir=self.remote_dir,
                         repo_dir=self.remote_dir, desc="测试仓库", sync=False,
                         root=self.root)

    def test_干跑不改线上任何东西(self):
        self._seed_remote([("hello", "1.0-r1"), ("hello", "1.0-r2")])
        before = sorted(p.name for p in self.remote_dir.iterdir())

        buf = io.StringIO()
        with redirect_stdout(buf):
            plan = prune.run(arch="x86_64", remote=self._remote(), go=False,
                             repo_dir=self.staging, root=self.root)

        self.assertEqual(len(plan.drop), 1)
        self.assertEqual([p.name for p in plan.drop], ["hello"])
        self.assertEqual(sorted(p.name for p in self.remote_dir.iterdir()), before,
                         "干跑不该动线上")
        self.assertIn("这是干跑", buf.getvalue())

    def test_go_删掉旧的那份_索引里只剩剩下的(self):
        self._seed_remote([("hello", "1.0-r1"), ("hello", "1.0-r2"),
                           ("world", "2.0-r1")])

        with redirect_stdout(io.StringIO()):
            plan = prune.run(arch="x86_64", remote=self._remote(), go=True,
                             repo_dir=self.staging, root=self.root)

        self.assertEqual([p.name for p in plan.drop], ["hello"])
        self.assertEqual(plan.drop[0].version, "1.0-r1")

        # ① 文件没了 / 该留的还在
        self.assertFalse((self.remote_dir / "hello-1.0-r1.apk").exists())
        self.assertTrue((self.remote_dir / "hello-1.0-r2.apk").is_file())
        self.assertTrue((self.remote_dir / "world-2.0-r1.apk").is_file())

        # ② 索引是从【剩下的集合】建的：拿它对一遍，一条问题都不该有
        entries, _, problems = repo.check_index(self.remote_dir)
        self.assertEqual(problems, [])
        self.assertEqual(sorted(entries), [("hello", "1.0-r2"), ("world", "2.0-r1")])
        # ③ 描述沿用线上那份
        self.assertIn("测试仓库",
                      repo.parse_index_description(repo.dump_index(self.remote_dir)))

    def test_范围_last_release_只删同上游的旧修订(self):
        self._seed_remote([("hello", "1.0-r1"), ("hello", "1.0-r2"),
                           ("hello", "2.0-r1")])

        with redirect_stdout(io.StringIO()):
            plan = prune.run(arch="x86_64", remote=self._remote(), go=True,
                             scope="last-release", repo_dir=self.staging,
                             root=self.root)

        self.assertEqual([(p.name, p.version) for p in plan.drop], [("hello", "1.0-r1")])
        self.assertTrue((self.remote_dir / "hello-1.0-r2.apk").is_file())
        self.assertTrue((self.remote_dir / "hello-2.0-r1.apk").is_file())
        self.assertFalse((self.remote_dir / "hello-1.0-r1.apk").exists())

    def test_只清一个包时别的包一个都不动(self):
        self._seed_remote([("hello", "1.0-r1"), ("hello", "1.0-r2"),
                           ("world", "2.0-r1"), ("world", "2.0-r2")])

        with redirect_stdout(io.StringIO()):
            prune.run(arch="x86_64", remote=self._remote(), go=True,
                      package="hello", repo_dir=self.staging, root=self.root)

        self.assertFalse((self.remote_dir / "hello-1.0-r1.apk").exists())
        # world 那两个：一个都没动（虽然它们也"该清"）
        self.assertTrue((self.remote_dir / "world-2.0-r1.apk").is_file())
        self.assertTrue((self.remote_dir / "world-2.0-r2.apk").is_file())

    def test_没有要删的时候不动索引(self):
        self._seed_remote([("hello", "1.0-r1")])
        index = self.remote_dir / repo.INDEX_NAME
        before = index.read_bytes()

        with redirect_stdout(io.StringIO()) as buf:
            plan = prune.run(arch="x86_64", remote=self._remote(), go=True,
                             repo_dir=self.staging, root=self.root)

        self.assertEqual(plan.drop, [])
        self.assertEqual(index.read_bytes(), before, "没东西要删就不该重传索引")


if __name__ == "__main__":
    unittest.main()
