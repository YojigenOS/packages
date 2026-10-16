#!/usr/bin/env python3
"""repo 的测试 —— 索引里到底有没有那些包，是这一组要回答的问题。

【这一组里最该有的两条】

① `test_索引里列出全部包_版本对得上`（要 docker）：真的 `apk mkpkg` 造几个包，
   真的 `apk mkndx` 建索引，再用真的 `apk adbdump` 把索引读回来对一遍。
   这是任务里点名要的那条。

② `test_pkgname_spec_原样传进去`（不用 docker）：`--pkgname-spec` 里那两个
   `${name}`/`${version}` **必须是字面量**，由 mkndx 自己替换。这个坑记在
   scripts/repo.sh 的注释里（原来靠单引号给容器里的 sh 看），而且
   「aarch64 那条路碰巧是对的——所以这个 bug 只在换架构时现形」——正是那种
   "碰巧对"的东西。所以这里把 argv 截下来，逐项钉住。

③ 解析和比对是纯函数（`parse_index_dump` / `parse_package_dump` / `diff_index`），
   它们用【真的 adbdump 输出】当输入测 —— 不需要 docker。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from skirtlib import repo
from skirtlib.repo import (diff_index, parse_index_dump, parse_package_dump)


def _docker_ready() -> bool:
    if shutil.which("docker") is None:
        return False
    r = subprocess.run(["docker", "image", "inspect", "alpine:latest"],
                       capture_output=True)
    return r.returncode == 0


def _openssl_ready() -> bool:
    return shutil.which("openssl") is not None


#: 真的 `apk adbdump APKINDEX.tar.gz` 输出（从头截到第二个包）——本仓库建出来的
#: var/repo/aarch64/APKINDEX.tar.gz 里那两条。
REAL_INDEX_DUMP = """\
#%SCHEMA: 78646E69
# ADB block, size: 17952, compat: 0, ver: 0
description: Yojigen OS aarch64 软件仓库
packages: # 60 items
  - name: apk-tools
    version: 3.0.8-r1
    hashes: 047217daf3992006abb74e11de3790db86a1e62bc00b647b360ffdfb5890cdcf
    description: 包管理器（Alpine Package Keeper）
    arch: aarch64
    license: GPL-2.0-only
    origin: apk-tools
    url: https://gitlab.alpinelinux.org/alpine/apk-tools
    installed-size: 623545
    file-size: 234432
    depends: # 10 items
      - ca-certificates
      - glibc
      - so:libz.so.1
    provides: # 1 items
      - so:libapk.so.3.0.0
  - name: dash-binsh
    version: 0.5.13.5-r1
    hashes: 81cc32d324cf5ae710fd2c480ab2f147ae4c34cc
    arch: aarch64
pkgname-spec: aarch64/${name}-${version}.apk
"""

#: 真的 `apk adbdump <某个 .apk>` 输出（dash 那个包）。
#: ★ 注意 `paths:` 里也有 `name:` —— 解析必须只认 info 段里那两个空格缩进的。
REAL_PACKAGE_DUMP = """\
#%SCHEMA: 676B6370
# ADB block, size: 688, compat: 0, ver: 0
info:
  name: dash
  version: 0.5.13.5-r1
  hashes: f0dc23adb167d859a8fd9ec290a946c2dd696b24
  description: POSIX shell（/bin/sh 指向它）
  arch: aarch64
  license: BSD-3-Clause AND GPL-2.0-or-later
  origin: dash
  url: http://gondor.apana.org.au/~herbert/dash/
  installed-size: 268238
paths: # 6 items
  - acl:
      mode: 0755
      user: nobody
  - name: usr
    acl:
      mode: 0755
  - name: usr/bin
    files: # 1 items
      - name: dash
        acl:
          mode: 0755
"""


class Test解析_adbdump_的渲染(unittest.TestCase):
    def test_索引里读出每一条的名字和版本(self):
        self.assertEqual(parse_index_dump(REAL_INDEX_DUMP),
                         [("apk-tools", "3.0.8-r1"),
                          ("dash-binsh", "0.5.13.5-r1")])

    def test_包的身份只认_info_那一段(self):
        """`paths:` 里有 `- name: usr` 和 `- name: dash`，都不能被当成包名。"""
        self.assertEqual(parse_package_dump(REAL_PACKAGE_DUMP), ("dash", "0.5.13.5-r1"))

    def test_读不出条目要炸_而不是返回空(self):
        """★ 返回空会让上面那条检查【看起来通过了】（0 条问题）。

        宁可炸：那是"检查了个寂寞"，比不检查更糟。
        """
        with self.assertRaises(ValueError):
            parse_index_dump("这不是 index 的渲染\n随便几行\n")

    def test_读不出身份就返回_None(self):
        self.assertIsNone(parse_package_dump("info:\n  arch: aarch64\n"))


class Test对账(unittest.TestCase):
    def test_全都对得上(self):
        entries = [("hello", "1.0-r0"), ("world", "2.0-r0")]
        ident = {"hello-1.0-r0.apk": ("hello", "1.0-r0"),
                 "world-2.0-r0.apk": ("world", "2.0-r0")}

        self.assertEqual(diff_index(entries, ident), [])

    def test_包没进索引(self):
        """仓库里有、索引里没列 —— 那个包等于没发布。"""
        entries = [("hello", "1.0-r0")]
        ident = {"hello-1.0-r0.apk": ("hello", "1.0-r0"),
                 "world-2.0-r0.apk": ("world", "2.0-r0")}

        problems = diff_index(entries, ident)

        self.assertEqual(len(problems), 1)
        self.assertIn("world-2.0-r0.apk", problems[0])
        self.assertIn("没进索引", problems[0])

    def test_版本对不上也算没进索引(self):
        """同名不同版本不算数 —— "数目对了内容不对"是这条要拦的。"""
        entries = [("hello", "1.1-r0")]
        ident = {"hello-1.0-r0.apk": ("hello", "1.0-r0")}

        problems = diff_index(entries, ident)

        self.assertEqual(len(problems), 2)      # 双向都对不上
        self.assertTrue(any("没进索引" in p for p in problems))
        self.assertTrue(any("仓库里没有这个包" in p for p in problems))

    def test_索引里有仓库里没有的包(self):
        entries = [("hello", "1.0-r0"), ("ghost", "9.9-r9")]
        ident = {"hello-1.0-r0.apk": ("hello", "1.0-r0")}

        problems = diff_index(entries, ident)

        self.assertEqual(len(problems), 1)
        self.assertIn("ghost", problems[0])


class Test收拢(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="repo-test-")
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.pkgs = self.tmp / "pkgs"
        self.repo_dir = self.tmp / "repo"
        self.pkgs.mkdir()
        self.repo_dir.mkdir()

    def _apk(self, name: str) -> Path:
        p = self.pkgs / name
        p.write_bytes(b"ADBd fake")
        return p

    def test_只复制_apk_而且不改源(self):
        self._apk("hello-1.0-r0.apk")
        self._apk("world-2.0-r0.apk")
        (self.pkgs / "notes.txt").write_text("不是包\n")

        staged = repo.stage(repo.apk_files(self.pkgs), self.repo_dir)

        self.assertEqual([p.name for p in staged],
                         ["hello-1.0-r0.apk", "world-2.0-r0.apk"])
        self.assertTrue((self.repo_dir / "hello-1.0-r0.apk").is_file())
        self.assertFalse((self.repo_dir / "notes.txt").exists())
        self.assertTrue((self.pkgs / "hello-1.0-r0.apk").is_file(),
                        "源文件要还在（cp 不是 mv）")

    def test_仓库里已有的包不会被删(self):
        """仓库是【只增不减】的：只在 pkgs 里的包被复制，别的不动。"""
        self._apk("hello-1.0-r0.apk")
        (self.repo_dir / "ancient-0.1-r1.apk").write_bytes(b"ADBd old")

        repo.stage(repo.apk_files(self.pkgs), self.repo_dir)

        self.assertTrue((self.repo_dir / "ancient-0.1-r1.apk").is_file())

    def test_没有凭据就不拉线上(self):
        self.assertFalse(repo.has_s3_credentials({}))
        self.assertFalse(repo.has_s3_credentials({"S3_BASE_URL": "x"}))
        self.assertTrue(repo.has_s3_credentials(
            {"S3_BASE_URL": "x", "S3_BUCKET": "b", "S3_AK": "k"}))

    def test_没有凭据时_sync_直接跳过(self):
        self.assertFalse(repo.sync_from_s3(self.repo_dir, "aarch64", env={}))


class Test签名记号(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="repo-mark-")
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_写上密钥名_再擦掉(self):
        p = repo.mark_signed(self.root, "x86_64", "yojigen-os-2026-1.rsa")

        self.assertEqual(p, self.root / "var/.repo-signed-x86_64")
        self.assertEqual(p.read_text(), "yojigen-os-2026-1.rsa\n")

        repo.unmark_signed(self.root, "x86_64")
        self.assertFalse(p.exists())

    def test_擦一个本来就没有的不报错(self):
        repo.unmark_signed(self.root, "aarch64")   # 不抛就算过


class Test命令行参数(unittest.TestCase):
    """★ 把 docker 的 argv 截下来看 —— 不用真的起容器。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="repo-argv-")
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.repo_dir = self.tmp / "repo"
        self.repo_dir.mkdir()
        (self.repo_dir / "hello-1.0-r0.apk").write_bytes(b"ADBd fake")

    def _run_mkndx(self, **kw) -> list[str]:
        captured: dict = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            (self.repo_dir / repo.INDEX_NAME).write_bytes(b"ADBd fake-index")
            return subprocess.CompletedProcess(cmd, 0, "Index has 1 packages", "")

        with mock.patch.object(repo.subprocess, "run", fake_run):
            repo.make_index(self.repo_dir, "x86_64",
                            desc="测试仓库", **kw)
        return captured["cmd"]

    def test_pkgname_spec_原样传进去_而且带架构(self):
        """`${name}`/`${version}` 必须是字面量，架构必须跟着 --arch 走。

        ★ 原来这两个是靠单引号"给容器里那个 sh 看"的。而
          「aarch64 那条路碰巧是对的——所以这个 bug 只在换架构时现形」
          —— 这条测试就是给那个碰巧的。
        """
        cmd = self._run_mkndx()

        spec = cmd[cmd.index("--pkgname-spec") + 1]
        self.assertEqual(spec, "x86_64/${name}-${version}.apk")
        self.assertEqual(cmd[cmd.index("--description") + 1], "测试仓库")
        self.assertEqual(cmd[cmd.index("--output") + 1], repo.INDEX_NAME)

    def test_不带密钥就走_allow_untrusted(self):
        cmd = self._run_mkndx()

        self.assertIn("--allow-untrusted", cmd)
        self.assertNotIn("--sign-key", cmd)

    def test_带密钥就挂密钥和公钥目录(self):
        key = self.tmp / "k.rsa"
        key.write_text("假装这是私钥")
        keys = self.tmp / "keys"
        keys.mkdir()

        cmd = self._run_mkndx(sign_key=key, keys_dir=keys)

        self.assertIn("--sign-key", cmd)
        self.assertEqual(cmd[cmd.index("--sign-key") + 1], "/key.rsa")
        self.assertIn("--keys-dir", cmd)
        self.assertIn(f"{key}:/key.rsa:ro", cmd)
        self.assertIn(f"{keys}:/keys:ro", cmd)

    def test_包名是逐个列出来的_不是靠_shell_展开(self):
        """没有 shell：`*.apk` 由 Python 展开。"""
        (self.repo_dir / "world-2.0-r0.apk").write_bytes(b"ADBd fake")

        cmd = self._run_mkndx()

        self.assertIn("hello-1.0-r0.apk", cmd)
        self.assertIn("world-2.0-r0.apk", cmd)
        self.assertNotIn("*.apk", cmd)
        self.assertIn("/repo", cmd[cmd.index("--workdir") + 1])

    def test_退出码非零要拦住(self):
        def fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 99, "ERROR: x.apk: UNTRUSTED signature"
                                                       "\nERROR: 1 errors, not creating index", "")

        with mock.patch.object(repo.subprocess, "run", fake_run):
            with self.assertRaises(SystemExit):
                repo.make_index(self.repo_dir, "aarch64", desc="x")

    def test_退出码是0但没有产物也要拦住(self):
        """★ 原来那个 ✓ 就是这么打出来的：管道吃掉了 mkndx 的报错。"""
        def fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with mock.patch.object(repo.subprocess, "run", fake_run):
            with self.assertRaises(SystemExit):
                repo.make_index(self.repo_dir, "aarch64", desc="x")


@unittest.skipUnless(_openssl_ready(), "需要 openssl")
class Test公钥推导(unittest.TestCase):
    def test_从私钥现场导出公钥(self):
        with tempfile.TemporaryDirectory(prefix="repo-keys-") as td:
            tmp = Path(td)
            key = tmp / "yojigen-test.rsa"
            subprocess.run(["openssl", "genrsa", "-out", str(key), "2048"],
                           capture_output=True, check=True)

            pub = repo.public_key(key, tmp)

            self.assertEqual(pub, tmp / "yojigen-test.rsa.pub")
            self.assertIn("BEGIN PUBLIC KEY", pub.read_text())

    def test_不是私钥就报错(self):
        with tempfile.TemporaryDirectory(prefix="repo-keys-") as td:
            tmp = Path(td)
            key = tmp / "not-a-key.rsa"
            key.write_text("这不是密钥\n")

            with self.assertRaises(SystemExit):
                repo.public_key(key, tmp)


# ------------------------------------------------------------------ 真的建索引（要 docker）

def _make_repo_pkg(tmp: Path, name: str, version: str, out_dir: Path,
                   *, key: Path | None = None) -> Path:
    """在容器里 `apk mkpkg` 造一个真的包，放进 out_dir。返回它的路径。"""
    src = tmp / f"src-{name}"
    (src / "usr/bin").mkdir(parents=True, exist_ok=True)
    (src / "usr/bin" / name).write_text(f"#!/bin/sh\necho {name} {version}\n")
    sign = "--sign-key /key.rsa " if key else ""
    mounts = ["-v", f"{tmp}:/w"]
    if key:
        mounts += ["-v", f"{key}:/key.rsa:ro"]
    out_rel = out_dir.relative_to(tmp)
    script = (f"apk mkpkg {sign}--info name:{name} --info version:{version} "
              f"--info arch:aarch64 --files /w/src-{name} "
              f"--output /w/{out_rel}/{name}-{version}.apk || exit 1\n"
              f"chown -R {os.getuid()}:{os.getgid()} /w\n")
    r = subprocess.run(["docker", "run", "--rm", "--entrypoint", "sh", *mounts,
                        "alpine:latest", "-c", script],
                       capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, f"造包失败：\n{r.stdout}{r.stderr}"
    return out_dir / f"{name}-{version}.apk"


@unittest.skipUnless(_docker_ready(), "需要 docker 和 alpine 镜像")
class Test真的建索引(unittest.TestCase):
    """真的 mkpkg + 真的 mkndx + 真的 adbdump。

    这一条就是任务里点名要的："至少验索引里列出了所有包、版本对得上"。
    """

    TIMEOUT = 300

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="repo-docker-")
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.repo_dir = self.tmp / "repo"
        self.repo_dir.mkdir()
        self.pkgs = self.tmp / "pkgs"
        self.pkgs.mkdir()

    def _build(self, **kw):
        """走 build_index 的完整那条路（不签名、不拉线上）。"""
        return repo.build_index(arch="aarch64", pkgs_dir=self.pkgs,
                                repo_dir=self.repo_dir,
                                desc="测试仓库", sync=False,
                                root=self.tmp, **kw)

    def test_索引里列出全部包_版本对得上(self):
        for name, ver in (("hello", "1.0-r0"), ("hello", "1.1-r0"),
                          ("world", "2.0-r0")):
            _make_repo_pkg(self.tmp, name, ver, self.pkgs)

        index = self._build()

        self.assertTrue(index.is_file() and index.stat().st_size > 0)
        entries, identities, problems = repo.check_index(self.repo_dir)
        self.assertEqual(problems, [], "索引和仓库对不上")
        self.assertEqual(sorted(entries),
                         [("hello", "1.0-r0"), ("hello", "1.1-r0"),
                          ("world", "2.0-r0")])
        # 每个包的身份也读得出来（那是上面那条比对的依据）
        self.assertEqual(sorted(identities.values()),
                         [("hello", "1.0-r0"), ("hello", "1.1-r0"),
                          ("world", "2.0-r0")])

    def test_少了一个包会被指出来(self):
        """★ 反着验一次：把索引换成"只有两个包"的那份，检查必须报出来。

        没有这条的话，上面那条可能只是"检查根本没在看"。
        """
        _make_repo_pkg(self.tmp, "hello", "1.0-r0", self.pkgs)
        _make_repo_pkg(self.tmp, "world", "2.0-r0", self.pkgs)

        self._build()
        # 索引建好之后又来一个包，而索引还是旧的 —— 这正是"发了包但没进索引"，
        # 也是"检查了个寂寞"最容易放过的那种。
        _make_repo_pkg(self.tmp, "late", "3.0-r0", self.repo_dir)

        _, _, problems = repo.check_index(self.repo_dir)

        self.assertEqual(len(problems), 1)
        self.assertIn("late-3.0-r0.apk", problems[0])

    @unittest.skipUnless(_openssl_ready(), "需要 openssl")
    def test_签名_索引真的被签了(self):
        """签名的判据是【索引里的签名块】，不是"我们传了 --sign-key"。

        `apk adbdump` 会把签名渲染成 `# sig v00 …` 那一行（那是渲染，不是文件里的
        字节 —— 所以只能这么查）。
        """
        key = self.tmp / "yojigen-test.rsa"
        subprocess.run(["openssl", "genrsa", "-out", str(key), "2048"],
                       capture_output=True, check=True)
        _make_repo_pkg(self.tmp, "hello", "1.0-r0", self.pkgs, key=key)

        index = self._build(sign_key=key)

        text = repo._dump(self.repo_dir)
        self.assertIn("# sig v00", text, "索引里没有签名块 —— 它没被签")
        # 记号给 `skirt deploy` 看（它没法自己从索引里读出来）
        self.assertEqual((self.tmp / "var/.repo-signed-aarch64").read_text(),
                         "yojigen-test.rsa\n")
        self.assertTrue(index.is_file())

    @unittest.skipUnless(_openssl_ready(), "需要 openssl")
    def test_没签的包配签名索引会被拦住(self):
        """mkndx 会逐个验证它索引的包：包没签而索引要签 → 直接拒绝建索引。

        这条同时验了两道检查（退出码 + 产物）里那道退出码。
        """
        key = self.tmp / "yojigen-test.rsa"
        subprocess.run(["openssl", "genrsa", "-out", str(key), "2048"],
                       capture_output=True, check=True)
        _make_repo_pkg(self.tmp, "plain", "1.0-r0", self.pkgs)      # ★ 没签

        with self.assertRaises(SystemExit):
            self._build(sign_key=key)

    def test_不签名时不留签名记号(self):
        _make_repo_pkg(self.tmp, "hello", "1.0-r0", self.pkgs)
        (self.tmp / "var").mkdir()
        (self.tmp / "var/.repo-signed-aarch64").write_text("上一个密钥.rsa\n")

        self._build()

        self.assertFalse((self.tmp / "var/.repo-signed-aarch64").exists(),
                         "不签名的这次要把上一次的记号擦掉")


class Test边车要跟着包一起走(unittest.TestCase):
    """★ 这条守的是一处**安静了很久**的漏搬（2026-10-16 补）。

    pack 写 var/pkgs/<名>.apk 和 <名>.apk.buildkey，而 stage 只搬了前者 ——
    于是线上没有 build_key，restore 就说「我不知道它上次是什么样」，
    **每轮 CI 都全量重建**（87 包 × 两架构 × 约两小时）。
    """

    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.pkgs = self.d / "pkgs"
        self.out = self.d / "repo"
        self.pkgs.mkdir()
        self.out.mkdir()

    def test_边车跟着走(self):
        (self.pkgs / "a-1.0-r1.apk").write_bytes(b"APK")
        (self.pkgs / "a-1.0-r1.apk.buildkey").write_text("key\n")
        repo.stage(repo.apk_files(self.pkgs), self.out)
        self.assertTrue((self.out / "a-1.0-r1.apk.buildkey").is_file(),
                        "边车没跟着搬 —— 线上就没有 build_key，于是每轮全量重建")

    def test_没有边车也不报错(self):
        (self.pkgs / "b-2.0-r1.apk").write_bytes(b"APK")
        repo.stage(repo.apk_files(self.pkgs), self.out)
        self.assertFalse(list(self.out.glob("b-*.buildkey")), "凭空造了一份边车")


if __name__ == "__main__":
    unittest.main()
