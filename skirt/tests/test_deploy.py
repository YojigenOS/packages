#!/usr/bin/env python3
"""deploy 的测试 —— "传什么、按什么顺序、用什么命令"这三件事的判定。

【为什么这一组要写得细】

部署这一步里有两条**踩出来**的性质，而它们在 shell 里只能靠读一遍代码才知道：

  ① **只 `copy`，绝不 `sync`。** 后者会删掉目标端多出来的文件 —— 那是不可逆的，
     而且它不是"上传"该做的决定（"删"由 `skirt prune` 管，决策记录 0020）。
  ② **先传包，后传索引。** apk 是先拿索引、再按索引取包；索引先到就会让客户端
     404。所以第一步失败时第二步【不跑】。

两条都在这里用 argv 钉住（不用网络、不用凭据、不用 docker），另加一条**真的**
跑 rclone 的组：拿一个本地目录当远端，验证"目标端多出来的文件不会被删"。

那些要联网/要凭据的路（真的 S3 上传）在这一组里没有测试 —— 它们只在 CI 上、
带着 secret 跑。这件事在报告里说清楚，而不是假装测过。
"""

from __future__ import annotations

import shutil
import subprocess
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from skirtlib import deploy, repo

S3_ENV = {
    "S3_AK": "AK",
    "S3_SK": "SK",
    "S3_BASE_URL": "http://s3.internal:9000",
    "S3_BUCKET": "yojigen-os-repo",
}


def _make_pkg(tmp: Path, out_dir: Path, name: str, version: str) -> None:
    """在容器里 `apk mkpkg` 造一个【真的】包。

    ★ 假的字节糊不过去：`apk mkndx` 会逐个验证它索引的包。这一条和
      `test_prune.py` 里那个同名 helper 是一回事（两个都在造"真包"这种测试
      夹具，谁也别抄谁的那份实现 —— 它只有五行）。
    """
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


def _rclone_image_ready() -> bool:
    if shutil.which("docker") is None:
        return False
    r = subprocess.run(["docker", "image", "inspect", deploy.RCLONE_IMAGE],
                       capture_output=True)
    return r.returncode == 0


class _Notes:
    """收集 `out=print` 那些行。"""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def __call__(self, *parts, **kw) -> None:
        self.lines.append(" ".join(str(p) for p in parts))

    def text(self) -> str:
        return "\n".join(self.lines)

    def any(self, needle: str) -> bool:
        return any(needle in line for line in self.lines)


class Test凭据(unittest.TestCase):
    def test_没有_S3_变量就什么都不设(self):
        self.assertEqual(deploy.rclone_env({}), {})
        # 三个里缺一个也不算有凭据（和 repo.has_s3_credentials 同一套判据）
        self.assertEqual(deploy.rclone_env({k: v for k, v in S3_ENV.items()
                                            if k != "S3_SK"}), {})

    def test_有凭据就拼成_RCLONE_CONFIG_那一套(self):
        env = deploy.rclone_env(S3_ENV)

        self.assertEqual(env["RCLONE_CONFIG_YOJIGEN_TYPE"], "s3")
        # ★ provider 必须是 Other：写 s3 的话它会去连 AWS 的 endpoint
        self.assertEqual(env["RCLONE_CONFIG_YOJIGEN_PROVIDER"], "Other")
        self.assertEqual(env["RCLONE_CONFIG_YOJIGEN_ENDPOINT"],
                         "http://s3.internal:9000")
        self.assertEqual(env["RCLONE_CONFIG_YOJIGEN_ACCESS_KEY_ID"], "AK")
        self.assertEqual(env["RCLONE_CONFIG_YOJIGEN_SECRET_ACCESS_KEY"], "SK")
        # ★ 路径式：RustFS / MinIO 都是，虚拟主机式通常没配 DNS
        self.assertEqual(env["RCLONE_CONFIG_YOJIGEN_FORCE_PATH_STYLE"], "true")
        self.assertEqual(env["RCLONE_CONFIG_YOJIGEN_REGION"], "us-east-1")

    def test_用的是具名_remote_的名字(self):
        """★ `yojigen:` —— 只有具名 remote 才读那些 RCLONE_CONFIG_* 变量。

        用 `:s3:` 连接字符串的话，rclone 会报 `s3 provider "" not known`，
        然后拿默认值去连（而那个错看起来像"凭据不对"）。
        """
        self.assertEqual(repo.REMOTE_NAME, "yojigen")
        self.assertEqual(deploy.resolve_remote(None, S3_ENV),
                         "yojigen:yojigen-os-repo")
        self.assertNotIn(":s3:", deploy.resolve_remote(None, S3_ENV))

    def test_命令行给的远端优先(self):
        self.assertEqual(deploy.resolve_remote("rustfs:yojigen-os-repo", S3_ENV),
                         "rustfs:yojigen-os-repo")

    def test_什么都没有就没有远端(self):
        self.assertIsNone(deploy.resolve_remote(None, {}))


class Test两步(unittest.TestCase):
    def setUp(self) -> None:
        self.steps = deploy.steps("aarch64", "rustfs:yojigen-os-repo",
                                  go=False, src="var/repo/aarch64/")

    def test_就两步_先包后索引(self):
        self.assertEqual(len(self.steps), 2)
        self.assertIn("不含索引", self.steps[0][0])
        self.assertIn("索引", self.steps[1][0])

    def test_第一步排除索引(self):
        argv = self.steps[0][1]

        self.assertIn("--exclude", argv)
        self.assertEqual(argv[argv.index("--exclude") + 1], repo.INDEX_NAME)

    def test_第二步只传索引(self):
        argv = self.steps[1][1]

        self.assertEqual(argv[argv.index("copy") + 1],
                         f"var/repo/aarch64/{repo.INDEX_NAME}")
        self.assertNotIn("--exclude", argv, "第二步本来就只传索引")
        self.assertNotIn("--s3-upload-cutoff", argv,
                         "索引不大，不需要分片那一套（那三个参数是给包用的）")
        self.assertIn("--checksum", argv)

    def test_目标路径带架构(self):
        for _, argv in self.steps:
            self.assertIn("rustfs:yojigen-os-repo/aarch64/", argv)

    def test_绝不用_sync(self):
        """★ 这一条是任务里点名要的：copy 不删目标端多出来的东西。"""
        for _, argv in self.steps:
            self.assertNotIn("sync", argv)
            self.assertNotIn("--delete", argv)
            self.assertNotIn("move", argv)
            self.assertEqual(argv[0], "copy")

    def test_传包那步有分片参数(self):
        """★ 大包会被反代按单个请求的大小挡掉（413）—— 强制分片绕过去。"""
        argv = self.steps[0][1]

        self.assertEqual(argv[argv.index("--s3-upload-cutoff") + 1], "8Mi")
        self.assertEqual(argv[argv.index("--s3-chunk-size") + 1], "5Mi")
        self.assertEqual(argv[argv.index("--s3-upload-concurrency") + 1], "4")
        self.assertIn("--checksum", argv)

    def test_干跑加_dry_run_真传不加(self):
        for _, argv in self.steps:
            self.assertIn("--dry-run", argv)

        real = deploy.steps("aarch64", "r:o", go=True, src="/repo/aarch64")

        for _, argv in real:
            self.assertNotIn("--dry-run", argv)

    def test_容器里那个源路径是挂载点(self):
        """用容器时源路径是 `/repo/<架构>` —— 路径要跟着挂载走。"""
        steps = deploy.steps("aarch64", "r:o", go=False, src="/repo/aarch64")

        self.assertEqual(steps[0][1][1], "/repo/aarch64")
        self.assertEqual(steps[1][1][1], f"/repo/aarch64/{repo.INDEX_NAME}")


class Test流程(unittest.TestCase):
    """用假的 runner 把 deploy() 跑一遍 —— 顺序、停在哪儿、退出码。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="deploy-flow-")
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.repo_dir = self.root / "repo"
        arch = self.repo_dir / "aarch64"
        arch.mkdir(parents=True)
        (arch / repo.INDEX_NAME).write_bytes(b"ADBd index")
        (arch / "hello-1.0-r1.apk").write_bytes(b"ADBd hello")
        (arch / "world-2.0-r1.apk").write_bytes(b"ADBd world")
        self.calls: list[list[str]] = []

    def _deploy(self, *, go=False, fail_on=(), out=None, **kw):
        def fake(args, **kwargs):
            self.calls.append(list(args))
            code = 1 if len(self.calls) in fail_on else 0
            return subprocess.CompletedProcess(args, code, "Transferred: 1 / 1", "")

        notes = out if out is not None else _Notes()
        rc = deploy.deploy("aarch64", "rustfs:yj", go=go, repo_dir=self.repo_dir,
                           env={}, runner=fake, local=False, out=notes, **kw)
        return rc, notes

    def test_两步都跑_顺序是包在前索引在后(self):
        rc, _ = self._deploy(go=True)

        self.assertEqual(rc, 0)
        self.assertEqual(len(self.calls), 2)
        first, second = self.calls
        self.assertFalse(first[1].endswith(repo.INDEX_NAME),
                         "第一步的【源】是整个架构目录（索引靠 --exclude 排除）")
        self.assertEqual(first[first.index("--exclude") + 1], repo.INDEX_NAME)
        self.assertTrue(second[1].endswith(repo.INDEX_NAME),
                        "第二步的【源】就是那一个索引文件")

    def test_第一步失败就不传索引(self):
        """★ 索引先到 = 客户端 404。所以失败时【不能】继续传索引。"""
        rc, notes = self._deploy(go=True, fail_on=(1,))

        self.assertEqual(rc, 1)
        self.assertEqual(len(self.calls), 1, "第二步不该跑")
        self.assertTrue(notes.any("索引【没有】传"))

    def test_第二步失败也算失败(self):
        rc, notes = self._deploy(go=True, fail_on=(2,))

        self.assertEqual(rc, 1)
        self.assertEqual(len(self.calls), 2)

    def test_干跑会说出来而且不报完成(self):
        rc, notes = self._deploy(go=False)

        self.assertEqual(rc, 0)
        self.assertTrue(notes.any("干跑"))
        self.assertFalse(notes.any("部署完成"))

    def test_没有索引就先报错(self):
        (self.repo_dir / "aarch64" / repo.INDEX_NAME).unlink()

        with self.assertRaises(SystemExit):
            self._deploy()

    def test_没有远端就说用法(self):
        with self.assertRaises(SystemExit):
            deploy.deploy("aarch64", None, go=False, repo_dir=self.repo_dir,
                          env={}, runner=lambda *a, **k: None, out=_Notes())

    def test_报出源里有多少个包(self):
        _, notes = self._deploy()

        self.assertTrue(notes.any("2 个包"), notes.text())

    def test_签名记号有就说有_没有就提醒(self):
        _, notes = self._deploy()
        self.assertTrue(notes.any("没有 —— 客户端"))

        marker = repo.mark_signed_path(Path(".").resolve(), "aarch64")
        self.assertTrue(str(marker).endswith("var/.repo-signed-aarch64"))


class Test签名记号落在哪(unittest.TestCase):
    def test_写和读走同一条路(self):
        """★ 路径只该有一处：写它的（skirt repo）和读它的（skirt deploy）。

        复制第二份的表现是静默的 —— 记号写在这儿、读的是那儿，于是部署那边
        永远报「没有签名」，而实际是签了的（旧脚本就查着一个不存在的文件）。
        """
        with tempfile.TemporaryDirectory(prefix="deploy-mark-") as td:
            root = Path(td)

            written = repo.mark_signed(root, "x86_64", "k.rsa")

            self.assertEqual(written, repo.mark_signed_path(root, "x86_64"))
            self.assertEqual(written.relative_to(root).as_posix(),
                             "var/.repo-signed-x86_64")


# ------------------------------------------------------------------ 真的跑一次 rclone（要 docker）

@unittest.skipUnless(_rclone_image_ready(), "需要 docker 和 rclone/rclone 镜像")
class Test端到端_本地目录当远端(unittest.TestCase):
    """拿一个本地目录当远端（rclone 的 `:local:` + 一个挂载），真的传一次。

    不需要 S3 凭据 —— 而 `:local:` 和 `yojigen:` 走的是同一个 rclone、同一套
    参数，所以"rclone 那条路"本身是被真跑过的。

    验两件事（都不需要网络）：

      · 干跑不动目标端
      · **`--go` 之后目标端多出来的文件还在** —— 这是"copy 不是 sync"在真的
        一次传输里的样子（我不是在断言 argv，是在看结果）
    """

    TIMEOUT = 600

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="deploy-e2e-")
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.repo_dir = self.root / "repo"
        self.arch_dir = self.repo_dir / "x86_64"
        self.arch_dir.mkdir(parents=True)
        self.data = self.root / "data"           # "线上"
        self.dest = self.data / "x86_64"
        self.dest.mkdir(parents=True)

        # 源：两个【真的】包 + 一份真的索引（索引用 skirt repo 自己的实现建，
        # 别抄一份 —— 它是从包文件生成的，而那句话对假包不成立）
        for name, version in (("hello", "1.0-r1"), ("world", "2.0-r1")):
            _make_pkg(self.root, self.arch_dir, name, version)
        with redirect_stdout(StringIO()):
            repo.build_index(arch="x86_64", pkgs_dir=self.arch_dir,
                             repo_dir=self.arch_dir, desc="测试仓库", sync=False,
                             root=self.root)

        # 目标端"上一轮传上去、这一轮的源里没有"的文件
        (self.dest / "ancient-0.1-r1.apk").write_bytes(b"ADBd ancient")

    def _deploy(self, *, go: bool):
        return deploy.deploy(
            "x86_64", self._remote(), go=go, repo_dir=self.repo_dir, env={},
            local=False, out=_Notes(),
            # 源挂 /repo、目标挂 /data；`env={}` 让 rclone 不去碰 S3 配置
            mounts=[(self.repo_dir, deploy.REPO_IN_CONTAINER),
                    (self.data, "/data")])

    def _remote(self) -> str:
        return ":local:/data"

    def test_干跑不改目标端(self):
        before = sorted(p.name for p in self.dest.iterdir())

        rc = deploy.deploy("x86_64", self._remote(), go=False,
                           repo_dir=self.repo_dir, env={}, local=False,
                           out=_Notes(),
                           mounts=[(self.repo_dir, deploy.REPO_IN_CONTAINER),
                                   (self.data, "/data")])

        self.assertEqual(rc, 0)
        self.assertEqual(sorted(p.name for p in self.dest.iterdir()), before)

    def test_go_之后包和索引都在_而且旧文件没被删(self):
        rc = self._deploy(go=True)

        self.assertEqual(rc, 0)
        names = sorted(p.name for p in self.dest.iterdir())
        self.assertEqual(names, sorted([repo.INDEX_NAME, "ancient-0.1-r1.apk",
                                        "hello-1.0-r1.apk", "world-2.0-r1.apk"]),
                         "copy 不该删掉目标端多出来的文件 —— 那是 sync 干的事")
        # 传上去的字节要和源【一模一样】（不是空文件、不是被改写过的）
        self.assertEqual((self.dest / "hello-1.0-r1.apk").read_bytes(),
                         (self.arch_dir / "hello-1.0-r1.apk").read_bytes())

    def test_传上去的索引能读懂(self):
        """★ 索引是【真的】传上去的那份 —— 不是空文件、不是半个。

        ★ 判据用 `apk adbdump`，不用"文件非空"：索引里列的东西是那个文件的
          全部意义，而"非空"说明不了它有没有把那两个包收进去。

        ★ 那个 extra 文件（目标端多出来的）**不在**索引里 —— 这恰好是这一组
          想展示的状态：copy 不删它，也不假装它是这次发布的。所以这里只对
          【索引里有什么】下断言，不拿它对全目录做完整的对账。
        """
        self.assertEqual(self._deploy(go=True), 0)

        dump = repo.dump_index(self.dest)

        self.assertEqual(sorted(repo.parse_index_dump(dump)),
                         [("hello", "1.0-r1"), ("world", "2.0-r1")])


if __name__ == "__main__":
    unittest.main()
