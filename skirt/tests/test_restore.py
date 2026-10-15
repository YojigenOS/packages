#!/usr/bin/env python3
"""restore 的测试 —— 那一半最容易错的是【名字和顺序】，所以那些先测。

【为什么值得这么多条】

`restore` 是从 `scripts/restore-payloads.sh` 搬过来的，而那个脚本的两处判定
都用一次【静默失效】换来过：

  · `.buildkey` 的源名字要带 `.apk`（`foo-1.0-r1.apk.buildkey`，不是
    `foo-1.0-r1.buildkey`）。写错的表现是"跳过判断说盘上没有 key" —— 离成因
    很远，因为两边只差三个字符。
  · 那个拷贝要在"payload 已经在了就跳过"【之前】。跳过的那批恰恰是最需要 key
    的（它们在 var/output 里已经有目录了）。

这两条现在各有一条测试钉着（`Test搬旁路文件`）。而两条都是可以在【没有
docker】的地方跑的：解包那一步被换成了一个假的 extractor，判定全在 Python 里
——这正是搬它的理由。

真解包（容器里的 `apk extract`）另有一组，标了 docker 的 skip。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from skirtlib import restore
from skirtlib.spec import all_recipe_names, load_spec, subpackages


def _docker_ready() -> bool:
    if shutil.which("docker") is None:
        return False
    r = subprocess.run(["docker", "image", "inspect", "alpine:latest"],
                       capture_output=True)
    return r.returncode == 0


class _TempTree(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="restore-test-")
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.pkgs = self.tmp / "var/pkgs/aarch64"
        self.out = self.tmp / "var/output/aarch64"

    def _apk(self, name: str, *, buildkey: str | None = None) -> Path:
        """放一个【空壳】.apk（内容无所谓：这些测试不解它）。"""
        self.pkgs.mkdir(parents=True, exist_ok=True)
        apk = self.pkgs / name
        apk.write_bytes(b"ADBd fake-apk-bytes")   # 内容无所谓：这些测试不解它
        if buildkey is not None:
            (self.pkgs / (name + ".buildkey")).write_text(buildkey)
        return apk


# ------------------------------------------------------------------ 名字 → 目录

class Test从配方表算版本(unittest.TestCase):
    def test_每个配方都在表里_值就是_版本_发布号(self):
        versions = restore.recipe_versions()
        names = all_recipe_names()
        self.assertGreater(len(names), 0)
        for name in names:
            pkg = load_spec(name)["package"]
            want = f"{pkg['version']}-{pkg['release']}"
            self.assertEqual(versions.get(pkg["name"]), want,
                             f"{pkg['name']} 的版本-发布号不对")

    def test_子包不进版本表_但算认识(self):
        """★ 子包【故意】不在版本表里 —— 见 plan() 那段：子包从文件名推才是对的。

        所以这里钉两件事：① 表里没有子包名；② 但 `subpackage_names()` 认识它们
        （"名字不在任何配方里"那个警告要靠这个区分"子包"和"真的不认识"）。
        """
        versions = restore.recipe_versions()
        subs = restore.subpackage_names()
        n_sub = 0
        for name in all_recipe_names():
            for sub in subpackages(load_spec(name)):
                n_sub += 1
                self.assertNotIn(sub["name"], versions,
                                 f"子包 {sub['name']} 不该进版本表")
                self.assertIn(sub["name"], subs)
        self.assertGreater(n_sub, 0,
                           "一条子包都没有？那这条测试什么都没验到")


class Test还原计划(_TempTree):
    def test_目录名里的_r_只出现在文件名里(self):
        """`bc-1.08.2-r1.apk` → 目录 `bc-1.08.2-1`。

        从文件名剥 `r` 是"复制 skirt 的命名规则"，所以这里走配方表。
        """
        self._apk("bc-1.08.2-r1.apk")

        jobs = restore.plan(self.pkgs, self.out, {"bc": "1.08.2-1"})

        self.assertEqual([j.rel for j in jobs], ["bc-1.08.2-1"])
        self.assertFalse(jobs[0].guessed)

    def test_子包落回它自己的目录名(self):
        """`glibc-dev` 这种子包在 var/output 里【有自己的一层】：

            var/output/aarch64/glibc-2.44-2/
            var/output/aarch64/glibc-dev-2.44-2/
            var/output/aarch64/glibc-locales-2.44-2/

        母配方建一次产出三个包，三个各占一个目录（collect.py 里
        `package_rel(name, pkg["version"], pkg["release"])` —— 名字用子包的、
        版本用母配方的）。

        ★ 这里用【真的配方表】：子包不在表里，所以它走的是"从文件名推"那条路
          （不是漏了，理由见 restore.plan() 的 docstring）。
        """
        for n in ("glibc-2.44-r2.apk", "glibc-dev-2.44-r2.apk",
                  "glibc-locales-2.44-r2.apk"):
            self._apk(n)

        jobs = restore.plan(self.pkgs, self.out, restore.recipe_versions())

        self.assertEqual([j.rel for j in jobs],
                         ["glibc-2.44-2", "glibc-dev-2.44-2", "glibc-locales-2.44-2"])
        # 子包走文件名那条路是【设计好的】，不该被当成"猜"
        self.assertFalse(any(j.guessed for j in jobs))

    def test_同名的老版本不会被塞进新版本的目录(self):
        """★ 这条是 2026-10-15 对着真数据核出来的（老脚本的行为，照搬）：

            var/pkgs 里同时有 libssl-3.5.9-r1.apk 和 r2，配方现在是 3.5.9-2
            → 子包各归各位：libssl-3.5.9-1 和 libssl-3.5.9-2
            → 如果子包也去查表，两个都会落到 libssl-3.5.9-2，"先到的赢"，
              于是 r2 的目录里装进 r1 的 payload —— 看起来正常，内容是旧的。
        """
        for n in ("libssl-3.5.9-r1.apk", "libssl-3.5.9-r2.apk"):
            self._apk(n)

        jobs = restore.plan(self.pkgs, self.out,
                            {"openssl": "3.5.9-2"},
                            subpackages={"libssl"})

        self.assertEqual([(j.apk.name, j.rel) for j in jobs],
                         [("libssl-3.5.9-r1.apk", "libssl-3.5.9-1"),
                          ("libssl-3.5.9-r2.apk", "libssl-3.5.9-2")])

    def test_同名的老版本配方包会撞到同一个目录_而且说得出来(self):
        """配方包（不是子包）走的是版本表，所以老版本会被映射到【当前】那个目录。

        ★ 这是老脚本的行为，搬家时照搬了。它在 CI 里基本无害（配方没动过的包
          才会被跳过，而版本变了就说明配方动过 → 会重建，重建会覆盖整个目录），
          但【说清楚】是必须的：下面那条 ★ 就是给这个的。
        """
        for n in ("bash-5.3-r1.apk", "bash-5.3-r2.apk"):
            self._apk(n)
        calls: list[str] = []

        def fake(todo, pkgs_dir, output_dir):
            calls.extend(j.apk.name for j in todo)
            for j in todo:
                j.payload(output_dir).mkdir(parents=True, exist_ok=True)
            return {j.rel: "ok" for j in todo}

        import io as _io
        from contextlib import redirect_stdout
        buf = _io.StringIO()
        with redirect_stdout(buf):
            ok, skip, fail = restore.restore(
                "aarch64", pkgs_dir=self.pkgs, output_dir=self.out,
                versions={"bash": "5.3-2"}, extractor=fake)

        self.assertEqual((ok, skip, fail), (1, 1, 0))
        # 排序在前的是 r1 → 解的是它，r2 算"已有"（老脚本在循环里也是这个顺序）
        self.assertEqual(calls, ["bash-5.3-r1.apk"],
                         "同一个目录只该解一次，而且是排在前面那个")
        self.assertIn("指向同一个目录 bash-5.3-2", buf.getvalue())

    def test_名字带连字符也剥得对(self):
        """`glibc-dev` 里的连字符不能被当成"版本从这里开始"。"""
        self._apk("glibc-dev-2.44-r2.apk")

        jobs = restore.plan(self.pkgs, self.out, {"glibc-dev": "2.44-2"})

        self.assertEqual([j.rel for j in jobs], ["glibc-dev-2.44-2"])

    def test_配方表里没有的名字退回从文件名推_并说出来(self):
        """配方删了、或者老产物，表里查不到 —— 那就退回 `-r2` → `-2`。

        ★ 这条路不理想，所以 plan 会把它标出来（`guessed`），restore() 打印。
        """
        self._apk("ghost-1.0-r3.apk")

        jobs = restore.plan(self.pkgs, self.out, {})

        self.assertEqual([j.rel for j in jobs], ["ghost-1.0-3"])
        self.assertTrue(jobs[0].guessed)

    def test_按文件名排序(self):
        for n in ("b-1-r1.apk", "a-1-r1.apk", "c-1-r1.apk"):
            self._apk(n)

        jobs = restore.plan(self.pkgs, self.out, {"a": "1-1", "b": "1-1", "c": "1-1"})

        self.assertEqual([j.apk.name for j in jobs],
                         ["a-1-r1.apk", "b-1-r1.apk", "c-1-r1.apk"])

    def test_payload_已经在了就是跳过(self):
        self._apk("hello-1.0-r1.apk")
        self._apk("world-2.0-r1.apk")
        (self.out / "hello-1.0-1/payload").mkdir(parents=True)

        jobs = restore.plan(self.pkgs, self.out, {"hello": "1.0-1", "world": "2.0-1"})

        self.assertEqual({j.rel: j.skip for j in jobs},
                         {"hello-1.0-1": True, "world-2.0-1": False})

    def test_buildkey_的源名字带着_apk(self):
        """★ 2026-10-15 那个坑：pack.sh 写的是 `<全名>.apk.buildkey`。

        而这里第一版找的是 `<去掉 .apk 的全名>.buildkey`（`basename f .apk`），
        于是永远找不到。
        """
        apk = self._apk("foo-1.0-r1.apk", buildkey="真的那份")
        # 诱饵：那个【错名字】的文件也存在，而且内容不一样
        (self.pkgs / "foo-1.0-r1.buildkey").write_text("错名字那份")

        job = restore.plan(self.pkgs, self.out, {"foo": "1.0-1"})[0]

        self.assertEqual(job.buildkey, self.pkgs / (apk.name + ".buildkey"))
        self.assertEqual(job.buildkey.read_text(), "真的那份")


# ------------------------------------------------------------------ 旁路文件

class Test搬旁路文件(_TempTree):
    def setUp(self) -> None:
        super().setUp()
        #: 假的解包器记下"它被要求解哪些"——跳过的那些不该出现在这里
        self.extracted: list[str] = []

    def _fake_extract(self, todo, pkgs_dir, output_dir):
        """假的解包：只建个目录、放一个记号，然后全都报 ok。"""
        self.extracted = [j.rel for j in todo]
        for j in todo:
            p = j.payload(output_dir)
            p.mkdir(parents=True, exist_ok=True)
            (p / ".fake").write_text("解开了")
        return {j.rel: "ok" for j in todo}

    def test_跳过的包也要拿到_key(self):
        """★★ 这条是那个坑的另一半：拷贝必须在"payload 已经在了"【之前】。

        因为已经在 var/output 里有一层目录的那批，恰恰是最需要 key 的——
        跳过判断（和 `skirt stale`）靠它认"上次是什么样"，而 meta.json 不跟着
        var/pkgs 的缓存走。
        """
        self._apk("hello-1.0-r1.apk", buildkey='{"key": "abc"}')
        # payload 已经在了 → 这个包会被跳过
        (self.out / "hello-1.0-1/payload").mkdir(parents=True)

        ok, skip, fail = restore.restore(
            "aarch64", pkgs_dir=self.pkgs, output_dir=self.out,
            versions={"hello": "1.0-1"}, extractor=self._fake_extract)

        self.assertEqual((ok, skip, fail), (0, 1, 0))
        self.assertEqual(self.extracted, [], "跳过的包不该被送去解包")
        self.assertEqual((self.out / "hello-1.0-1/.buildkey").read_text(),
                         '{"key": "abc"}')

    def test_要解包的包也拿到_key(self):
        self._apk("hello-1.0-r1.apk", buildkey='{"key": "abc"}')

        ok, _, _ = restore.restore(
            "aarch64", pkgs_dir=self.pkgs, output_dir=self.out,
            versions={"hello": "1.0-1"}, extractor=self._fake_extract)

        self.assertEqual(ok, 1)
        self.assertEqual((self.out / "hello-1.0-1/.buildkey").read_text(),
                         '{"key": "abc"}')
        self.assertTrue((self.out / "hello-1.0-1/payload/.fake").is_file())

    def test_没有旁路文件就什么都不拷_并说出来(self):
        self._apk("hello-1.0-r1.apk")

        import io as _io
        from contextlib import redirect_stdout
        buf = _io.StringIO()
        with redirect_stdout(buf):
            restore.restore("aarch64", pkgs_dir=self.pkgs, output_dir=self.out,
                            versions={"hello": "1.0-1"}, extractor=self._fake_extract)

        self.assertFalse((self.out / "hello-1.0-1/.buildkey").exists())
        self.assertIn("没有 .buildkey", buf.getvalue())


# ------------------------------------------------------------------ 整套（不用 docker）

class Test整套流程(_TempTree):
    def _extract_ok(self, todo, pkgs_dir, output_dir):
        for j in todo:
            p = j.payload(output_dir)
            p.mkdir(parents=True, exist_ok=True)
            (p / "marker").write_text(j.apk.name)
        return {j.rel: "ok" for j in todo}

    def _extract_one_fails(self, todo, pkgs_dir, output_dir):
        done = {}
        for i, j in enumerate(todo):
            if i == 0:
                done[j.rel] = "fail"
                continue
            p = j.payload(output_dir)
            p.mkdir(parents=True, exist_ok=True)
            done[j.rel] = "ok"
        return done

    def test_数得清(self):
        for n in ("a-1-r1.apk", "b-1-r1.apk", "c-1-r1.apk"):
            self._apk(n)
        (self.out / "c-1-1/payload").mkdir(parents=True)      # c 是"已有"
        import io as _io
        from contextlib import redirect_stdout
        buf = _io.StringIO()
        with redirect_stdout(buf):
            ok, skip, fail = restore.restore(
                "aarch64", pkgs_dir=self.pkgs, output_dir=self.out,
                versions={"a": "1-1", "b": "1-1", "c": "1-1"},
                extractor=self._extract_ok)

        self.assertEqual((ok, skip, fail), (2, 1, 0))
        self.assertIn("还原 2 个，已有 1 个，失败 0 个", buf.getvalue())

    def test_解不开的包算失败(self):
        self._apk("a-1-r1.apk")
        self._apk("b-1-r1.apk")

        import io as _io
        from contextlib import redirect_stdout
        buf = _io.StringIO()
        with redirect_stdout(buf):
            ok, skip, fail = restore.restore(
                "aarch64", pkgs_dir=self.pkgs, output_dir=self.out,
                versions={"a": "1-1", "b": "1-1"},
                extractor=self._extract_one_fails)

        self.assertEqual((ok, skip, fail), (1, 0, 1))

    def test_没有_pkgs_目录(self):
        import io as _io
        from contextlib import redirect_stdout
        buf = _io.StringIO()
        with redirect_stdout(buf):
            result = restore.restore("aarch64", pkgs_dir=self.pkgs,
                                     output_dir=self.out, versions={},
                                     extractor=self._extract_ok)

        self.assertEqual(result, (0, 0, 0))
        self.assertIn("没什么可还原的", buf.getvalue())

    def test_pkgs_目录是空的(self):
        self.pkgs.mkdir(parents=True)

        import io as _io
        from contextlib import redirect_stdout
        buf = _io.StringIO()
        with redirect_stdout(buf):
            result = restore.restore("aarch64", pkgs_dir=self.pkgs,
                                     output_dir=self.out, versions={},
                                     extractor=self._extract_ok)

        self.assertEqual(result, (0, 0, 0))
        self.assertIn("没有 .apk", buf.getvalue())

    def test_产物目录会被建出来(self):
        self._apk("a-1-r1.apk")

        import io as _io
        from contextlib import redirect_stdout
        with redirect_stdout(_io.StringIO()):
            restore.restore("aarch64", pkgs_dir=self.pkgs, output_dir=self.out,
                            versions={"a": "1-1"}, extractor=self._extract_ok)

        self.assertTrue(self.out.is_dir())


# ------------------------------------------------------------------ 真解包（要 docker）

@unittest.skipUnless(_docker_ready(), "需要 docker 和 alpine 镜像")
class Test真解包(unittest.TestCase):
    """真的 `.apk`（容器里 `apk mkpkg` 造）+ 真的 `apk extract`。

    一条都不跳的话，这一组验的是"还原出来的目录对不对"这件事本身——
    上面那些测试只能验"我们打算去哪解、解完怎么记"。
    """

    TIMEOUT = 300

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="restore-docker-")
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.pkgs = self.tmp / "var/pkgs/aarch64"
        self.out = self.tmp / "var/output/aarch64"
        self.pkgs.mkdir(parents=True)

    def _mkpkg(self, name: str, version: str, files: dict[str, str]) -> None:
        """造一个真的 .apk，里面装着 files（相对路径 → 内容）。"""
        src = self.tmp / f"src-{name}"
        for rel, text in files.items():
            p = src / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
        script = (
            f"apk mkpkg --info name:{name} --info version:{version} "
            f"--info arch:aarch64 --files /w/src-{name} "
            f"--output /w/var/pkgs/aarch64/{name}-{version}.apk || exit 1\n"
            f"chown -R {os.getuid()}:{os.getgid()} /w\n")
        r = subprocess.run(
            ["docker", "run", "--rm", "--entrypoint", "sh", "-v", f"{self.tmp}:/w",
             "alpine:latest", "-c", script],
            capture_output=True, text=True, timeout=self.TIMEOUT)
        self.assertEqual(r.returncode, 0, f"造包失败：\n{r.stdout}{r.stderr}")

    def _real_extract(self, todo, pkgs_dir, output_dir):
        return restore.extract_with_apk(todo, pkgs_dir, output_dir)

    def test_真解包_跳过_buildkey三件事一起验(self):
        self._mkpkg("hello", "1.0-r1", {"usr/bin/hello": "#!/bin/sh\necho hi\n"})
        self._mkpkg("world", "2.0-r1", {"usr/bin/world": "world\n"})
        # hello 有旁路文件；world 的 payload 已经在了（要跳过）
        (self.pkgs / "hello-1.0-r1.apk.buildkey").write_text('{"key": "abc"}')
        (self.out / "world-2.0-1/payload").mkdir(parents=True)
        (self.out / "world-2.0-1/payload/留给它").write_text("本来就有的\n")

        import io as _io
        from contextlib import redirect_stdout
        buf = _io.StringIO()
        with redirect_stdout(buf):
            ok, skip, fail = restore.restore(
                "aarch64", pkgs_dir=self.pkgs, output_dir=self.out,
                versions={"hello": "1.0-1", "world": "2.0-1"},
                extractor=self._real_extract)

        self.assertEqual((ok, skip, fail), (1, 1, 0), "汇总不对")

        # ① 还原出来的目录对不对
        got = (self.out / "hello-1.0-1/payload/usr/bin/hello").read_text()
        self.assertEqual(got, "#!/bin/sh\necho hi\n")

        # ② 已经在了的没被动过（解包不会往它里面写东西）
        w = self.out / "world-2.0-1/payload"
        self.assertEqual((w / "留给它").read_text(), "本来就有的\n")
        self.assertFalse((w / "usr").exists(), "跳过的包不该被解开")

        # ③ .buildkey 跟着回来了
        self.assertEqual((self.out / "hello-1.0-1/.buildkey").read_text(),
                         '{"key": "abc"}')


if __name__ == "__main__":
    unittest.main()
