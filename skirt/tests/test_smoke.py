#!/usr/bin/env python3
"""smoke 的测试 —— 六项检查各自的【判据】。

【为什么这一组值得写】

`skirt smoke` 本身要几分钟、要 docker —— 所以它不会进单元测试那条路。但它的
六项检查每一条都是"某个东西坏了要被指出来"，而其中一条（第⑥项）尤其：

    读一个已经打好的包，断言里面【只有一种 mtime】，而且它不是最近一小时内的。

这条判断错一点点（比如"看解包出来的时间戳"而不是"看 apk adbdump 的渲染"）
就会**假阳性**——而那正是 2026-10-15 那天真的踩过的：解包器不恢复符号链接和
目录的时间戳，于是"只修了一半"这个错结论看起来有证据。判据本身值得有测试。

所以这里两个层次都测：

    · 判据（纯函数）：`judge_probe` / `judge_mtime` / `payload_shape` /
      `parse_mtimes` / `parse_collisions` / `newest_apk`
    · 整条编排：用一个假 runner 喂罐头输出，走完六项 —— 不用 docker
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from skirtlib import smoke


def _docker_ready() -> bool:
    if shutil.which("docker") is None:
        return False
    r = subprocess.run(["docker", "image", "inspect", "alpine:latest"],
                       capture_output=True)
    return r.returncode == 0


class Test第一项_大小写碰撞(unittest.TestCase):
    def test_读出那个数字(self):
        self.assertEqual(smoke.parse_collisions("13\n"), 13)
        self.assertEqual(smoke.parse_collisions("docker 说了点什么\n13\n"), 13)

    def test_读不出来当_0(self):
        self.assertEqual(smoke.parse_collisions(""), 0)
        self.assertEqual(smoke.parse_collisions("没有数字\n"), 0)

    def test_解压在容器里做(self):
        """★ 判据是"解压发生在哪边" —— 所以那段脚本必须跑在容器里，而且
        解压用的是容器自己的文件系统（`/x`，不是挂载进来的宿主目录）。
        """
        script = smoke.collision_script()

        self.assertIn("tar -xf /src.tar -C /x", script)
        self.assertIn("/x", script)
        self.assertIn("lower()", script, "数的是大小写碰撞")


class Test第三项_产物形态(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="smoke-shape-")
        self.payload = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _binary(self, mode: int = 0o755) -> Path:
        p = self.payload / "usr/bin" / smoke.PROBE
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("#!/bin/sh\necho hi\n")
        p.chmod(mode)
        return p

    def test_空产物算空(self):
        count, ok = smoke.payload_shape(self.payload)

        self.assertEqual(count, 0)
        self.assertFalse(ok)

    def test_有东西而且可执行才算好(self):
        self._binary()

        count, ok = smoke.payload_shape(self.payload)

        self.assertEqual(count, 1)
        self.assertTrue(ok)

    def test_探针名是传下来的_不是写死的(self):
        """★ 写死的话，换成别的探针包时这一格查的还是 hello —— 而它会报成功。"""
        binary = self.payload / "usr/bin" / "glibc"
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_text("#!/bin/sh\n")
        binary.chmod(0o755)

        count, ok = smoke.payload_shape(self.payload, probe="glibc")

        self.assertEqual(count, 1)
        self.assertTrue(ok)
        # 而默认那个探针不在这儿 —— 不能算它过
        self.assertFalse(smoke.payload_shape(self.payload)[1])

    def test_存在但不可执行要报出来(self):
        """★ 「在」和「能跑」是两件事（那一格原来就是"缺了个执行位"）。"""
        self._binary(0o644)

        _, ok = smoke.payload_shape(self.payload)

        self.assertFalse(ok)


class Test第四_五项_探针输出(unittest.TestCase):
    REAL = "EN=Hello, world!\nZH=世界你好\nTW=哈囉\n"

    def test_三行都读得回来(self):
        self.assertEqual(smoke.parse_probe_output(self.REAL),
                         {"EN": "Hello, world!", "ZH": "世界你好", "TW": "哈囉"})

    def test_三样都对(self):
        results = smoke.judge_probe(self.REAL)

        self.assertTrue(all(ok for ok, _ in results), results)

    def test_英文不对要报出来(self):
        results = smoke.judge_probe("EN=Hello\nZH=世界你好\nTW=哈囉\n")

        self.assertFalse(results[0][0])
        self.assertIn("不对", results[0][1])

    def test_中文没出来要说清是三者之一断了(self):
        """★ 这一格是「中文优先」那个承诺的可执行定义。"""
        results = smoke.judge_probe("EN=Hello, world!\nZH=Hello, world!\nTW=哈囉\n")

        ok, text = results[1]
        self.assertFalse(ok)
        self.assertIn("gettext", text)
        self.assertIn("NLS", text)
        self.assertIn("locale", text)

    def test_容器什么都没输出时三样都算不对(self):
        results = smoke.judge_probe("")

        self.assertEqual([ok for ok, _ in results], [False, False, False])


class Test第六项_可复现的归一化(unittest.TestCase):
    """★ 任务里点名的那一条：一个包里只有一个 mtime，而且不是最近一小时内的。"""

    NOW = 1_800_000_000.0

    def test_只有一个_而且是很久以前的_就通过(self):
        ok, text = smoke.judge_mtime([1_761_000_000] * 100, sample="hello.apk",
                                     now=self.NOW)

        self.assertTrue(ok, text)
        self.assertIn("只有一个 mtime", text)

    def test_两种_mtime_就算没归一化(self):
        ok, text = smoke.judge_mtime([1_761_000_000, 1_761_000_001],
                                     sample="hello.apk", now=self.NOW)

        self.assertFalse(ok)
        self.assertIn("归一化没生效", text)

    def test_刚出炉的_mtime_说明它抄的是墙钟(self):
        """★ 这一条是"用 0 糊过去"和"忘了传 SOURCE_DATE_EPOCH"之外最容易漏的：
        时间戳【有】，但它是构建那一刻的 —— 可复现就已经破了。
        """
        fresh = int(self.NOW - 60)

        ok, text = smoke.judge_mtime([fresh], sample="hello.apk", now=self.NOW)

        self.assertFalse(ok)
        self.assertIn("一小时之内", text)
        self.assertIn("墙钟", text)

    def test_边界_正好一小时算通过(self):
        """窗口是"最近一小时之内"——正好一小时（不含）之外算通过。"""
        ok, _ = smoke.judge_mtime([int(self.NOW) - smoke.FRESH_WINDOW],
                                  sample="x.apk", now=self.NOW)

        self.assertTrue(ok)

    def test_一个_mtime_都没有要报出来(self):
        ok, text = smoke.judge_mtime([], sample="hello.apk", now=self.NOW)

        self.assertFalse(ok)
        self.assertIn("读不出", text)

    def test_判据是_adbdump_不是解包(self):
        """★ 解析的是 `apk adbdump` 的渲染（`      mtime: 1791778546`）。

        为什么不能"解开看时间戳"：解包器不恢复符号链接和目录的时间戳 ——
        "解出来时间不对"会假阳性（2026-10-15 那次就是这么得出"只修了一半"的）。
        """
        # ★ 这段是【真的渲染】：`apk adbdump` 把 mtime 打成 acl 块里的一个键，
        #   而 paths 里那些 `- name:` 是条目名（别把它们当 mtime）。
        dump = ("#%SCHEMA: 676B6370\n"
                "info:\n"
                "  name: hello\n"
                "  version: 2.12.3-r1\n"
                "paths: # 3 items\n"
                "  - acl:\n"
                "      mode: 0755\n"
                "  - name: usr\n"
                "    acl:\n"
                "      mode: 0755\n"
                "  - name: usr/bin\n"
                "    files: # 1 items\n"
                "      - name: hello\n"
                "        acl:\n"
                "          mode: 0755\n"
                "        size: 67856\n"
                "        mtime: 1791778546\n")

        self.assertEqual(smoke.parse_mtimes(dump), [1791778546])

    def test_只认_mtime_那一行(self):
        junk = "size: 123\n  notmtime: 5\n  mtime: 12\nxmtime: 99\n"

        self.assertEqual(smoke.parse_mtimes(junk), [12])


class Test找到最近的包(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="smoke-apk-")
        self.pkgs = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_没有包返回_None(self):
        self.assertIsNone(smoke.newest_apk(self.pkgs))

    def test_取最后修改的那个(self):
        (self.pkgs / "aarch64").mkdir()
        old = self.pkgs / "aarch64" / "old-1.0-r1.apk"
        new = self.pkgs / "aarch64" / "new-1.0-r1.apk"
        old.write_bytes(b"ADBd old")
        new.write_bytes(b"ADBd new")
        os.utime(old, (time.time() - 1000, time.time() - 1000))

        self.assertEqual(smoke.newest_apk(self.pkgs), new)


class Test整条编排(unittest.TestCase):
    """用假 runner 喂罐头输出，把六项走一遍（不用 docker）。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="smoke-run-")
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.out_dir = self.root / "output" / "aarch64"
        self.pkgs = self.root / "pkgs" / "aarch64"
        self.pkgs.mkdir(parents=True)

        self.lines: list[str] = []
        # 默认是"一切正常"的罐头输出；每条测试只覆盖它要动的那一条。
        self.answers: dict[str, tuple[int, str]] = {
            # ① 的容器回报：13 组大小写碰撞
            "/src.tar": (0, "13\n"),
            # ④⑤ 的容器回报
            "/in/loc": (0, "EN=Hello, world!\nZH=世界你好\nTW=哈囉\n"),
            # ⑥ 的 adbdump：一种 mtime，而且是很久以前的。
            # ★ 这段是【照真的渲染抄的】（`apk adbdump` 把 mtime 打成 acp 块里的
            #   一个键，前面没有 `- `）—— 夹具长得不像真的，测的就是别的东西了。
            "adbdump": (0, "info:\n  name: hello\npaths: # 2 items\n"
                           "  - name: usr/bin\n    files: # 2 items\n"
                           "      - name: hello\n        acl:\n"
                           "          mode: 0755\n        mtime: 1761000000\n"
                           "      - name: world\n        acl:\n"
                           "          mode: 0755\n        mtime: 1761000000\n"),
        }

    # ── 罐头输出 ─────────────────────────────────────────────────────────
    def _canned(self, argv, capture_output=True, text=True):
        key = " ".join(str(a) for a in argv)
        for frag, (code, out) in self.answers.items():
            if frag in key:
                return subprocess.CompletedProcess(argv, code, out, "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    def _seed_products(self, *, empty=False, executable=True) -> None:
        """造产物目录。`empty=True` 就是【一个文件都没有】的那种空产物。"""
        payload = self.out_dir / f"{smoke.PROBE}-2.12.3-1" / "payload"
        (payload / "usr/bin").mkdir(parents=True)
        if not empty:
            binary = payload / "usr/bin" / smoke.PROBE
            binary.write_text("#!/bin/sh\n")
            binary.chmod(0o755 if executable else 0o644)
            (payload / "usr/share").mkdir()
            (payload / "usr/share" / "hello.txt").write_text("hi\n")
        loc = self.out_dir / "glibc-locales-2.44-1" / "payload"
        loc.mkdir(parents=True)

    def _run(self, **kw):
        patches = [
            mock.patch.object(smoke, "OUTPUT_DIR", self.root / "output"),
            mock.patch.object(smoke, "PKGS_DIR", self.root / "pkgs"),
            mock.patch.object(smoke, "BUILD_DIR", self.root / "work"),
            mock.patch.object(smoke, "rmtree_force", lambda p: None),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return smoke.smoke("aarch64", out=self.lines.append,
                           runner=self._canned, **kw)

    # ── 六项都通过 ───────────────────────────────────────────────────────
    def test_六项都通过(self):
        self._seed_products()
        apk = self.pkgs / "hello-2.12.3-r1.apk"
        apk.write_bytes(b"ADBd hello")
        self.answers["build hello"] = (0, "建好了\n")

        rc = self._run(kernel_tar=Path("/tmp/fake-linux.tar.xz"))

        self.assertEqual(rc, 0, "\n".join(self.lines))

    def test_构建失败就让整条失败(self):
        self._seed_products()
        self.answers["build hello"] = (1, "gcc 说不行\n")

        rc = self._run()

        self.assertEqual(rc, 1)
        self.assertTrue(any("构建失败" in l for l in self.lines))

    def test_产物是空的要指出来(self):
        self._seed_products(empty=True)
        self.answers["build hello"] = (0, "")

        rc = self._run()

        self.assertEqual(rc, 1)
        self.assertTrue(any("产物是空的" in l for l in self.lines), self.lines)

    def test_可执行位丢了要指出来(self):
        self._seed_products(executable=False)
        self.answers["build hello"] = (0, "")

        rc = self._run()

        self.assertEqual(rc, 1)
        self.assertTrue(any("缺失或不可执行" in l for l in self.lines), self.lines)

    def test_没有内核_tarball_就跳过那项_而不是失败(self):
        self._seed_products()

        rc = self._run()

        self.assertEqual(rc, 0, "\n".join(self.lines))
        self.assertTrue(any("跳过：没有内核 tarball" in l for l in self.lines))

    def test_没有打过包就跳过第六项(self):
        self._seed_products()

        self.assertEqual(self._run(), 0, "\n".join(self.lines))

        self.assertTrue(any("跳过：var/pkgs 里没有 .apk" in l for l in self.lines))

    def test_有了包但时间戳是墙钟_要失败(self):
        """★ 这一条是第⑥项存在的全部理由：链上没别的地方会告诉你。"""
        self._seed_products()
        (self.pkgs / "hello-2.12.3-r1.apk").write_bytes(b"ADBd hello")
        fresh = int(time.time() - 60)
        self.answers["build hello"] = (0, "")
        self.answers["adbdump"] = (0, f"paths: # 1 items\n  - name: hello\n"
                                         f"    acl:\n      mode: 0755\n"
                                         f"    mtime: {fresh}\n")

        rc = self._run()

        self.assertEqual(rc, 1)
        self.assertTrue(any("墙钟" in l for l in self.lines), self.lines)


class Test调的是哪个构建(unittest.TestCase):
    def test_build_argv_调自己(self):
        argv = smoke.build_argv("hello", "x86_64")

        self.assertEqual(argv[1].endswith("skirt/skirt"), True)
        self.assertEqual(argv[2:], ["build", "hello", "--arch", "x86_64"])

    def test_挂进容器的路径必须是绝对的(self):
        """★ `docker run -v 相对路径:/x` 不报错 —— 它把那个相对路径当成
        【具名卷的名字】，于是 /x 挂的是一个空卷。

        症状是"容器里那个文件不见了"（adbdump 读不出 mtime、tar 说没有那个
        包），而离成因很远 —— 所以三个 argv 生成器都先 `resolve()`。
        """
        cases = [smoke.adbdump_argv(Path("some/relative.apk")),
                 smoke.collision_argv(Path("rel/x.tar.xz"), "img"),
                 smoke.probe_argv(Path("rel/p"), Path("rel/l"), "img")]

        for argv in cases:
            src = argv[argv.index("-v") + 1].split(":")[0]
            self.assertTrue(src.startswith("/"),
                            f"挂载的源路径必须是绝对的，现在挂的是：{src}")
        # 相对路径解析成【当前目录下】那份，不是别的地方
        mount = cases[0][cases[0].index("-v") + 1]
        self.assertEqual(mount, f"{Path('some/relative.apk').resolve()}:/p.apk:ro")

    def test_读包用的是_alpine_不是_buildenv(self):
        """devel 里没有 apk（实测 `command -v apk` 空）——读包得有 apk-tools。"""
        argv = smoke.adbdump_argv(Path("/tmp/x.apk"))

        self.assertIn(smoke.APK_IMAGE, argv)
        self.assertIn("adbdump", argv)


@unittest.skipUnless(_docker_ready(), "需要 docker 和 alpine 镜像")
class Test真的_ADBDUMP_读_mtime(unittest.TestCase):
    """真的用 `apk adbdump` 读一个真的包 —— 判据那一头也要对着真的东西验一次。

    这个包用 `SOURCE_DATE_EPOCH` 打一次（`apk mkpkg`），于是它应该读出
    "只有一种 mtime、而且不是现在"。这一条和第⑥项走的是同一条路，只是包是
    当场造的（不需要真的建一个配方）。
    """

    EPOCH = 1761000000

    def test_真的包读得出一种_mtime(self):
        with tempfile.TemporaryDirectory(prefix="smoke-apk-") as td:
            tmp = Path(td)
            (tmp / "in/usr/bin").mkdir(parents=True)
            (tmp / "in/usr/bin/hello").write_text("#!/bin/sh\necho hi\n")
            (tmp / "out").mkdir()
            script = ("apk mkpkg --info name:hello --info version:1.0-r1 "
                      "--files /w/in --output /w/out/hello-1.0-r1.apk")
            r = subprocess.run(
                ["docker", "run", "--rm", "-e", f"SOURCE_DATE_EPOCH={self.EPOCH}",
                 "--entrypoint", "sh", "-v", f"{tmp}:/w", "alpine:latest",
                 "-c", script],
                capture_output=True, text=True, timeout=300)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

            apk = tmp / "out/hello-1.0-r1.apk"
            dump = subprocess.run(smoke.adbdump_argv(apk), capture_output=True,
                                  text=True, timeout=300).stdout

            ok, text = smoke.judge_mtime(smoke.parse_mtimes(dump),
                                         sample=apk.name)

        self.assertTrue(ok, text)
        self.assertEqual(set(smoke.parse_mtimes(dump)), {self.EPOCH})


if __name__ == "__main__":
    unittest.main()
