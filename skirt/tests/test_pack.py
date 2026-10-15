#!/usr/bin/env python3
"""pack 的测试 —— 重点是"打出来的包里是什么"这件事的判定部分。

【为什么这一组要写得厚】

打包这一步决定【交付物的字节】：mkpkg 收哪些字段、时间戳从哪来、属主和能力位
怎么恢复、旁路文件叫什么名字。而它原来的形态是 442 行 shell：判定和容器调用
混在一起，于是"改了它、产物变了，而没人知道"这件事真的发生过（2026-10-15，
那一天 pack.sh 改了两次，两次都没进 build_key —— 见 build_key 那组测试）。

所以这里两种测试都要有：

    · 不用 docker 的：选产物、时间戳、mkpkg 字段、生成出来的 inner.sh、
      宿主上那条 docker argv、旁路文件的命名与搬运 —— 几十毫秒
    · 要 docker 的（`Test真的打包`）：真的 `apk mkpkg` 打一个包，再用真的
      `apk adbdump` 读回来 —— 身份、mtime 归一化、能力位、安装脚本

★ 那一组还顺带守着一条【跨模块】的东西：`adbdump` 里只能有一种 mtime，
  而且它等于我们喂进去的 SOURCE_DATE_EPOCH。`skirt smoke` 的第⑥项用的是
  同一个判据（见 smoke.py 顶上），只是它读的是已经躺在 var/pkgs 里的包。
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from skirtlib import pack
from skirtlib.pack import PackError


def _docker_ready() -> bool:
    if shutil.which("docker") is None:
        return False
    r = subprocess.run(["docker", "image", "inspect", "alpine:latest"],
                       capture_output=True)
    return r.returncode == 0


class _Said:
    """收集 `note` 的回调。

    ★ 为什么要个类而不是 `list.append`：`pack_one` 打进度那一行用的是
      `note(text, end="")`（先打前缀、跑完容器再补结果），而 `list.append`
      收不了关键字参数。测试里要的是"它说了什么"，所以这里照单全收。
    """

    def __init__(self) -> None:
        self.lines: list[str] = []

    def __call__(self, *parts, **kw) -> None:
        self.lines.append(" ".join(str(p) for p in parts))

    def any(self, needle: str) -> bool:
        return any(needle in line for line in self.lines)


def _docker_pack_ready() -> bool:
    """真的打一个包 —— 要 docker，还要 `SKIRT_TEST_PACK=1`。

    理由见 `Test真的打包` 的 docstring：这一步的容器里会 `apk add`（libcap +
    rsync），而那条路要连网络。网络好的时候一次十几秒，不好的时候同一个调用
    五分钟（apk 自己在重试超时）。默认跳过，想真验一次就打开开关。
    """
    return os.environ.get("SKIRT_TEST_PACK") == "1" and _docker_ready()


# ------------------------------------------------------------------ 造产物（不用容器）

def _meta(name: str = "hello", version: str = "2.12.3", release: int = 1,
          **extra) -> dict:
    m = {"name": name, "version": version, "release": release, "arch": "aarch64",
         "description": "GNU Hello，打招呼的示例程序", "license": "GPL-3.0-or-later",
         "url": "https://www.gnu.org/software/hello/", "depends": ["glibc"],
         "built_from": name}
    m.update(extra)
    return m


def _product(root: Path, meta: dict, *, files=("usr/bin/hello",)) -> Path:
    d = root / f"{meta['name']}-{meta['version']}-{meta['release']}"
    (d / "payload").mkdir(parents=True)
    (d / "meta.json").write_text(json.dumps(meta, ensure_ascii=False),
                                 encoding="utf-8")
    for rel in files:
        p = d / "payload" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("#!/bin/sh\necho hello\n", encoding="utf-8")
    return d


class Test选产物(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="pack-select-")
        self.out = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.arch_dir = self.out / "aarch64"
        self.arch_dir.mkdir()
        for name in ("hello-2.12.3-1", "hello-2.12.3-9", "hello-2.12.3-10",
                     "hello-world-1.0-1", "glibc-2.44-1"):
            (self.arch_dir / name).mkdir()

    def test_指定名字取最新的那个(self):
        """★ `-r9` 和 `-r10`：字典序会把 9 排在 10 后面，而那是打错版本。

        实测会碰到的形状：同一个包连着改几轮，release 到两位数。
        """
        got = pack.select_products("aarch64", ["hello"], output_dir=self.out)

        self.assertEqual([p.name for p in got], ["hello-2.12.3-10"])

    def test_名字后面必须跟数字(self):
        """`hello` 不能匹配到 `hello-world` —— 拷的是 shell 那个 `-[0-9]*`。"""
        got = pack.select_products("aarch64", ["hello"], output_dir=self.out)

        self.assertNotIn("hello-world-1.0-1", [p.name for p in got])

    def test_不指定名字就是全部(self):
        got = pack.select_products("aarch64", None, output_dir=self.out)

        self.assertEqual([p.name for p in got],
                         ["glibc-2.44-1", "hello-2.12.3-1", "hello-2.12.3-10",
                          "hello-2.12.3-9", "hello-world-1.0-1"])

    def test_指的名字一个都没有就是空(self):
        self.assertEqual(pack.select_products("aarch64", ["nope"],
                                              output_dir=self.out), [])

    def test_没有这个架构就是空(self):
        self.assertEqual(pack.select_products("riscv64", None,
                                              output_dir=self.out), [])


class Test装配单(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="pack-plan-")
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_apk_名字和目录名只差一个后缀(self):
        d = _product(self.root, _meta())

        job = pack.plan(d)

        self.assertEqual(job.apk, "hello-2.12.3-r1.apk")
        self.assertEqual(job.version, "2.12.3-r1", "mkpkg 的 version 要含发布号")
        self.assertEqual(job.payload, d / "payload")

    def test_没有_meta_json_就返回_None(self):
        d = self.root / "hello-2.12.3-1"
        d.mkdir()

        self.assertIsNone(pack.plan(d))


class Test时间戳(unittest.TestCase):
    """★ 这一组是任务里点名的第③条：拿不到时间戳就【响亮地失败】。"""

    def test_source_date_epoch_是整数就用它(self):
        self.assertEqual(pack.epoch_of(_meta(source_date_epoch=1760000000)),
                         "1760000000")

    def test_只有_built_at_的路要留着_而且要说出来(self):
        """历史产物只有 `built_at` —— 不留这条路，它们全部打不出包。

        而"我用了退路"必须说出来：那意味着那个包的时间戳本来就不确定。

        ★ 这个值是 `datetime.fromisoformat(...).timestamp()` 换出来的同一个
          （"2026-10-12T12:15:46+08:00" = 2026-10-12T04:15:46Z）。带上时区偏移
          的写法是确定的；而不带偏移的那种（`2026-10-12T12:15:46`）会按机器
          的本地时区解释 —— 那正是"它本来就不确定"这句话的另一半。
        """
        said = _Said()
        ts = pack.epoch_of(_meta(built_at="2026-10-12T12:15:46+08:00"), note=said)

        self.assertEqual(ts, "1791778546")
        self.assertTrue(said.any("本来就不确定"),
                        f"要说清这是退路，现在只说：{said.lines}")

    def test_两个都没有就停住(self):
        said = _Said()
        with self.assertRaises(PackError) as ctx:
            pack.epoch_of(_meta(), note=said)

        self.assertIn("没法保证可复现", str(ctx.exception))
        self.assertTrue(said.any("不打了"), said.lines)

    def test_built_at_是垃圾也要停住_不拿_0_糊(self):
        """★ 这里原来会退化成 `source_date_epoch=0`。

        `0` 是个【合法的时间戳】，所以它不会报错 —— 只会静默产出一个
        1970 年、而且和输入无关的包。那正是"可复现性破口"的形状。
        """
        with self.assertRaises(PackError):
            pack.epoch_of(_meta(built_at="不是个时间"))

    def test_built_at_是空串也要停住(self):
        with self.assertRaises(PackError):
            pack.epoch_of(_meta(built_at=""))


class TestELF字段(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="pack-elf-")
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _scan(self, needed, provided):
        def fake(_payload):
            return needed, provided
        return fake

    def test_两端都补上(self):
        """depends 里的 so: 靠 provides 里的 so: 解析 —— 只做一端会少一半。"""
        payload = self.root / "payload"
        payload.mkdir()

        depends, provides, note = pack.elf_fields(
            _meta(), payload,
            scanner=self._scan(["libc.so.6"], {"libz.so.1": "1.3.2"}))

        self.assertEqual(depends, ["glibc", "so:libc.so.6"])
        self.assertEqual(provides, ["so:libz.so.1=1.3.2"])
        self.assertIn("+1 个 so: 依赖", note)
        self.assertIn("+1 个 so: 提供", note)

    def test_手写和自动的可以并存_而且去重(self):
        payload = self.root / "payload"
        payload.mkdir()

        depends, provides, _ = pack.elf_fields(
            _meta(depends=["glibc", "so:libc.so.6"], provides=["so:libz.so.1=1.3.2"]),
            payload, scanner=self._scan(["libc.so.6"], {"libz.so.1": "1.3.2"}))

        self.assertEqual(depends, ["glibc", "so:libc.so.6"])
        self.assertEqual(provides, ["so:libz.so.1=1.3.2"])

    def test_没有_so_的时候不动它(self):
        payload = self.root / "payload"
        payload.mkdir()

        depends, provides, note = pack.elf_fields(_meta(), payload,
                                                  scanner=self._scan([], {}))

        self.assertEqual(depends, ["glibc"])
        self.assertEqual(provides, [])
        self.assertEqual(note, "", "没有 so: 就不要打那句说明")

    def test_provides_没有版本后缀就是光名字(self):
        payload = self.root / "payload"
        payload.mkdir()

        _, provides, _ = pack.elf_fields(_meta(), payload,
                                         scanner=self._scan([], {"libfoo.so.1": ""}))

        self.assertEqual(provides, ["so:libfoo.so.1"])


class Testmkpkg字段(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="pack-fields-")
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _job(self, **extra) -> pack.Job:
        return pack.plan(_product(self.root, _meta(**extra)))

    def test_空值不传(self):
        """mkpkg 对空字符串会报错 —— 所以空字段要【不出现】，不是传个空串。"""
        fields = pack.info_fields(self._job(url="", license=""), ["glibc"], [])

        keys = [k for k, _ in fields]
        self.assertNotIn("url", keys)
        self.assertNotIn("license", keys)
        self.assertNotIn("provides", keys)
        self.assertIn("name", keys)

    def test_必填的名字和版本一定在(self):
        fields = dict(pack.info_fields(self._job(), ["glibc"], []))

        self.assertEqual(fields["name"], "hello")
        self.assertEqual(fields["version"], "2.12.3-r1")
        self.assertEqual(fields["origin"], "hello", "origin 取的是 built_from")

    def test_provides_和_provider_priority_有才传(self):
        fields = dict(pack.info_fields(self._job(provider_priority=10),
                                       ["glibc"], ["so:libz.so.1=1.3.2"]))

        self.assertEqual(fields["provides"], "so:libz.so.1=1.3.2")
        self.assertEqual(fields["provider-priority"], "10")

    def test_depends_是空格拼起来的(self):
        fields = dict(pack.info_fields(self._job(), ["glibc", "so:libc.so.6"], []))

        self.assertEqual(fields["depends"], "glibc so:libc.so.6")


def _mkpkg_lines(script: str) -> list[list[str]]:
    """把 inner.sh 里那条 `exec apk mkpkg … \\` 拼回成一个 argv（用 shlex 解）。

    ★ 用 shlex 解而不是字符串包含 —— 因为"引号对不对"正是这段代码要保证的
      东西（描述里有中文、空格、逗号）。字符串包含测不出那个。
    """
    text = script.replace("\\\n", " ")
    for line in text.splitlines():
        if "apk mkpkg" in line:
            return shlex.split(line)
    raise AssertionError("inner.sh 里没有 apk mkpkg 那一行")


class Testinner_script(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="pack-inner-")
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _job(self, **extra) -> pack.Job:
        return pack.plan(_product(self.root, _meta(**extra)))

    def test_能解析出来的_mkpkg_命令(self):
        argv = _mkpkg_lines(pack.inner_script(self._job(), ["glibc", "so:libc.so.6"],
                                              ["so:libz.so.1=1.3.2"], signed=False))

        self.assertEqual(argv[:3], ["exec", "apk", "mkpkg"])
        self.assertEqual(argv[argv.index("--files") + 1], "/data")
        self.assertEqual(argv[argv.index("--output") + 1], "/out/hello-2.12.3-r1.apk")
        self.assertEqual(argv[argv.index("--info") + 1], "name:hello")
        self.assertIn("depends:glibc so:libc.so.6", argv)
        self.assertIn("provides:so:libz.so.1=1.3.2", argv)
        self.assertNotIn("--sign-key", argv)

    def test_描述里的中文空格逗号不会被拆开(self):
        """★ 这段脚本存在的理由就是这个：直接拼命令行会被吃掉引号。"""
        argv = _mkpkg_lines(pack.inner_script(self._job(), [], [], signed=False))

        self.assertIn("description:GNU Hello，打招呼的示例程序", argv)

    def test_签名时才带_sign_key(self):
        signed = _mkpkg_lines(pack.inner_script(self._job(), [], [], signed=True))

        self.assertEqual(signed[signed.index("--sign-key") + 1], "/key.rsa")

    def test_安装脚本按名字挂到_in_scripts(self):
        """产物目录的 scripts/post-install → mkpkg 的 --script post-install:/in/…"""
        d = _product(self.root, _meta(name="openssh"))
        (d / "scripts").mkdir()
        (d / "scripts" / "post-install").write_text("#!/bin/sh\nexit 0\n")
        (d / "scripts" / "post-upgrade").write_text("#!/bin/sh\nexit 0\n")

        argv = _mkpkg_lines(pack.inner_script(pack.plan(d), [], [], signed=False))

        got = [argv[i + 1] for i, a in enumerate(argv) if a == "--script"]
        self.assertEqual(got, ["post-install:/in/scripts/post-install",
                               "post-upgrade:/in/scripts/post-upgrade"])
        # 而脚本名就是 mkpkg 的脚本类型 —— 里面不能带路径
        self.assertTrue(all(":" in g and "/in/scripts/" in g for g in got))

    def test_能力位在_chown_之后设(self):
        """★ 顺序是判据：`chown` 会清掉 security.capability。

        写反了的症状不是报错 —— 是能力位【安静地没了】，而装完之后那个程序
        跑不起来（ping 没 cap_net_raw 就是这种）。
        """
        script = pack.inner_script(self._job(), [], [], signed=False)

        chown = script.index("chown -R 0:0 /data")
        setcap_from_list = script.index('done < "$CAPS"')
        self.assertLess(chown, setcap_from_list,
                        "能力位必须在 chown 之后设，否则会被 chown 清掉")

    def test_主路是文本清单_而且是payload里那份(self):
        script = pack.inner_script(self._job(), [], [], signed=False)

        self.assertIn('CAPS=/data/.yojigen-caps', script,
                      "主路是 build.py 写进 payload 的那份清单")
        self.assertIn('rm -f "$CAPS"', script, "清单本身不能进包")
        self.assertIn("setcap", script)

    def test_没有那段过时的_caps_txt(self):
        """★ 搬过来时收窄的一处，见 pack.py 顶上 ①。

        原来那段 getcap 记账 + setcap 恢复【被复制了两遍】，第二遍的末尾读的是
        `/in/caps.txt` —— 而全项目没有任何地方写那个文件（build.py 写的是
        payload 里的 `.yojigen-caps`）。它多跑一次 chown、多打一句
        「⚠ 没有 /in/caps.txt」，净效果为零却把人往错的方向指。
        """
        script = pack.inner_script(self._job(), [], [], signed=False)

        self.assertNotIn("/in/caps.txt", script)
        self.assertEqual(script.count("chown -R 0:0 /data"), 1)

    def test_setuid_位记下来再放回去(self):
        script = pack.inner_script(self._job(), [], [], signed=False)

        self.assertIn("-perm -4000 -o -perm -2000", script)
        self.assertIn("< /tmp/.setid", script)
        self.assertIn("chmod", script)

    def test_拷贝用rsync_而退回_cp_的时候要说出来(self):
        script = pack.inner_script(self._job(), [], [], signed=False)

        self.assertIn("rsync -aX", script)
        self.assertIn("退回 cp -a", script, "用了哪个要说出来，不静默")


class Test宿主上的argv(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="pack-argv-")
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.product = _product(self.root, _meta(source_date_epoch=1760000000))
        self.job = pack.plan(self.product)

    def _argv(self, **kw) -> list[str]:
        packdir = pack.pack_dir(self.root / "work", self.product)
        return pack.host_argv(self.job, pkgs_dir=self.root / "pkgs",
                              packdir=packdir, epoch="1760000000", **kw)

    def test_时间戳走环境变量(self):
        """`--info builddate:…` 是错的：mkpkg 报 invalid info field。"""
        argv = self._argv(sign_key=None)

        self.assertIn("SOURCE_DATE_EPOCH=1760000000", argv)
        self.assertNotIn("builddate", " ".join(argv))

    def test_三个挂载(self):
        argv = self._argv(sign_key=None)

        self.assertIn(f"{self.product}:/in:ro", argv)
        self.assertIn(f"{self.root / 'pkgs'}:/out", argv)
        self.assertIn(f"{self.root / 'work' / 'pack' / self.product.name}:/pack:ro",
                      argv)

    def test_签名密钥只在签的时候挂(self):
        self.assertNotIn(":/key.rsa:ro", " ".join(self._argv(sign_key=None)))

        key = self.root / "k.rsa"
        key.write_text("假装这是私钥")
        self.assertIn(f"{key}:/key.rsa:ro", self._argv(sign_key=key))


class Test每个包一个pack目录(unittest.TestCase):
    """★ macOS 上踩出来的：所有包共用一个 pack 目录 → 读到【上一个包】的 inner.sh。

        /pack/inner.sh: line 13: syntax error: unterminated quoted string

    而宿主上那个文件是对的。症状是【随机的包】失败 —— 每跑一次换一批。
    """

    def test_两个包的目录不一样(self):
        root = Path("/tmp/whatever")
        a = pack.pack_dir(root / "work", Path("var/output/aarch64/hello-2.12.3-1"))
        b = pack.pack_dir(root / "work", Path("var/output/aarch64/glibc-2.44-1"))

        self.assertNotEqual(a, b)
        self.assertEqual(a.name, "hello-2.12.3-1", "路径里要能看出是哪个包")

    def test_挂的是那个唯一的目录(self):
        root = Path("/tmp/whatever")
        product = Path("var/output/aarch64/hello-2.12.3-1")
        packdir = pack.pack_dir(root, product)

        self.assertIn(f"{packdir}:/pack:ro", pack.host_argv(
            pack.Job(product=product, meta={}, name="hello", version="x", apk="x.apk"),
            pkgs_dir=Path("/out"), packdir=packdir, epoch="0", sign_key=None))


class Test旁路文件的命名(unittest.TestCase):
    """★ 任务里点名的第①条：`.apk` 要留在名字【中间】。"""

    def test_apk_在中间(self):
        self.assertEqual(pack.sidecar_name("zlib-1.3.2-r1.apk"),
                         "zlib-1.3.2-r1.apk.buildkey")

    def test_不是剥掉_apk_的那个名字(self):
        """剥掉它那次让"还原 payload"静默失效过 —— 两边差三个字符。"""
        self.assertNotEqual(pack.sidecar_name("zlib-1.3.2-r1.apk"),
                            "zlib-1.3.2-r1.buildkey")


class Test打包流程_假容器(unittest.TestCase):
    """用假的 runner 把整条流程跑一遍 —— 容器那一步换掉，别的都是真的。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="pack-flow-")
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.out = self.root / "output"
        self.pkgs = self.root / "pkgs"
        (self.out / "aarch64").mkdir(parents=True)

    def _pack(self, *, produce=True, names=("hello",), **kw):
        def fake(argv, capture_output=True, text=True):
            if produce:
                apk = self.pkgs / "hello-2.12.3-r1.apk"
                apk.parent.mkdir(parents=True, exist_ok=True)
                apk.write_bytes(b"ADBd fake")
            return subprocess.CompletedProcess(argv, 0 if produce else 1,
                                               "mkpkg 说了什么", "")
        note = kw.pop("note", lambda *a, **k: None)
        return pack.pack("aarch64", list(names), output_dir=self.out,
                         pkgs_dir=self.pkgs, runner=fake, note=note, **kw)

    def test_成功时把_buildkey_搬到_apk_旁边(self):
        d = _product(self.out / "aarch64", _meta(source_date_epoch=1760000000))
        (d / ".buildkey").write_text('{"key":"abc"}')

        ok, fail, skip = self._pack(note=lambda *a, **k: None)

        self.assertEqual((ok, fail, skip), (1, 0, 0))
        dest = self.pkgs / "hello-2.12.3-r1.apk.buildkey"
        self.assertEqual(dest.read_text(), '{"key":"abc"}',
                         "旁路文件的名字必须带 .apk —— restore 按那个名字找")

    def test_没有_buildkey_就说出来_而且删掉旧的(self):
        _product(self.out / "aarch64", _meta(source_date_epoch=1760000000))
        stale = self.pkgs / "hello-2.12.3-r1.apk.buildkey"
        stale.parent.mkdir(parents=True, exist_ok=True)
        stale.write_text('{"key":"上一次的"}')
        said = _Said()

        self._pack(note=said)

        self.assertFalse(stale.exists(), "对不上的旧那份要删掉，免得骗人")
        self.assertTrue(said.any("没有 .buildkey"),
                        f"拿不准要说出来，别假装有（它说：{said.lines}）")

    def test_退出码是0但没有产物也算失败(self):
        """★ 退出码为 0 不等于产出了东西 —— 这条是踩出来的，两道检查都要。"""
        _product(self.out / "aarch64", _meta(source_date_epoch=1760000000))

        ok, fail, _ = self._pack(produce=False)

        self.assertEqual((ok, fail), (0, 1))

    def test_没有_meta_json_的产物算跳过(self):
        (self.out / "aarch64" / "hello-2.12.3-1").mkdir(parents=True)

        ok, fail, skip = self._pack()

        self.assertEqual((ok, fail, skip), (0, 0, 1))

    def test_拿不到时间戳时整个停下来(self):
        """★ 不是"记一个失败继续打下一个"：那说明 meta.json 这一代有问题。"""
        _product(self.out / "aarch64", _meta())        # 既没有 epoch 也没有 built_at

        with self.assertRaises(PackError):
            self._pack()

    def test_没有产物目录就报错(self):
        with self.assertRaises(PackError) as ctx:
            pack.pack("aarch64", ["hello"], output_dir=self.out, pkgs_dir=self.pkgs,
                      runner=lambda *a, **k: None, note=lambda *a, **k: None)

        self.assertIn("先 skirt build", str(ctx.exception))

    def test_既没给名字也没给_all_就报错(self):
        with self.assertRaises(PackError):
            pack.pack("aarch64", [], output_dir=self.out, pkgs_dir=self.pkgs,
                      runner=lambda *a, **k: None, note=lambda *a, **k: None)


# ------------------------------------------------------------------ 真的打包（要 docker）

def _adbdump(apk: Path) -> str:
    """在容器里把包渲染出来（`apk adbdump`）—— 读包的唯一可靠方式。"""
    r = subprocess.run(
        ["docker", "run", "--rm", "-v", f"{apk}:/p.apk:ro", "alpine:latest",
         "sh", "-c", "apk adbdump /p.apk 2>&1"],
        capture_output=True, text=True, timeout=300)
    return r.stdout


def _mtimes(dump: str) -> list[int]:
    out = []
    for line in dump.splitlines():
        line = line.strip()
        if line.startswith("mtime: "):
            out.append(int(line[len("mtime: "):]))
    return out


@unittest.skipUnless(_docker_pack_ready(), "需要 docker + alpine 镜像，"
                                           "而且要用 SKIRT_TEST_PACK=1 打开")
class Test真的打包(unittest.TestCase):
    """真的 `apk mkpkg` 打一个包，再用真的 `apk adbdump` 读回来。

    ★ 为什么要一个开关（`SKIRT_TEST_PACK=1`）而不只是 `_docker_ready()`：

      这一步起容器，而容器里那两句 `apk add`（libcap + rsync）要连 Alpine 的
      仓库。实测本机（2026-10-15）那里时好时坏：好的时候一次打包 13 秒，
      坏的时候同一个调用 5 分钟（apk 自己在重试超时），而打出来的包只有几百
      字节。单元测试套件不该被网络拖着走 —— 所以默认跳过，想真验一次就打开它：

          SKIRT_TEST_PACK=1 python3 -m unittest skirt.tests.test_pack -v

      默认的发现式跑法里，这一组计入 skip（和 `test_repo` / `test_prune` 那
      几组的写法一致：要 docker 的组都挂在 skip 上）。

    一次打包给好几条断言用，所以 setUpClass 里只打两回：一回带安装脚本、
    `.buildkey` 和能力位清单（每一样都能在 adbdump 里看到），一回是
    【同一个产物再打一次】—— 那一条验的是字节可复现。
    """

    EPOCH = 1760000000        # 2025-10-09 那种固定值：只要不是"现在"

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory(prefix="pack-docker-")
        cls.root = Path(cls._tmp.name)
        cls.out = cls.root / "output"
        cls.pkgs = cls.root / "pkgs"
        cls.pkgs2 = cls.root / "pkgs2"
        (cls.out / "aarch64").mkdir(parents=True)

        d = _product(cls.out / "aarch64",
                     _meta(name="iputils", version="20250605", release=1,
                           source_date_epoch=cls.EPOCH),
                     files=("usr/bin/ping",))
        (d / "scripts").mkdir()
        (d / "scripts" / "post-install").write_text(
            "#!/bin/sh\necho 装完了\n", encoding="utf-8")
        (d / ".buildkey").write_text('{"key":"deadbeef"}', encoding="utf-8")
        (d / "payload" / ".yojigen-caps").write_text(
            "/usr/bin/ping cap_net_raw=ep\n", encoding="utf-8")

        buf = StringIO()
        with redirect_stdout(buf):
            cls.result = pack.pack("aarch64", ["iputils"], output_dir=cls.out,
                                   pkgs_dir=cls.pkgs, runner=subprocess.run)
            # 同一个产物、同一个时间戳，再打一次（打进另一个目录，才能比字节）
            cls.result2 = pack.pack("aarch64", ["iputils"], output_dir=cls.out,
                                    pkgs_dir=cls.pkgs2, runner=subprocess.run)
        cls.output = buf.getvalue()

        cls.apk = cls.pkgs / "iputils-20250605-r1.apk"
        cls.apk2 = cls.pkgs2 / "iputils-20250605-r1.apk"
        cls.dump = _adbdump(cls.apk)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def test_打出来了_而且两次都成功(self):
        self.assertEqual(self.result, (1, 0, 0), self.output)
        self.assertEqual(self.result2, (1, 0, 0), self.output)
        self.assertGreater(self.apk.stat().st_size, 0)

    def test_身份读回来对得上(self):
        self.assertIn("name: iputils", self.dump)
        self.assertIn("version: 20250605-r1", self.dump)

    def test_mtime_归一成一个值_而且等于_SOURCE_DATE_EPOCH(self):
        """★ 和第⑥项冒烟检查同一个判据：`apk mkpkg` 把所有条目的 mtime 重设成
        SOURCE_DATE_EPOCH —— 那是整条链上唯一的归一化，没有第二处守着它。
        """
        self.assertTrue(self.dump.strip(), "adbdump 什么都没读到")

        got = _mtimes(self.dump)

        self.assertTrue(got, "包里一个 mtime 都没有？（adbdump 的输出变了？）")
        self.assertEqual(set(got), {self.EPOCH},
                         f"包里有 {len(set(got))} 种 mtime —— 归一化没生效")

    def test_安装脚本进了包(self):
        """--script 那条路：名字是脚本类型，路径是 /in/scripts/<名字>。"""
        self.assertIn("scripts", self.dump)
        self.assertIn("post-install", self.dump)

    def test_清单文件本身没进包(self):
        """`.yojigen-caps` 是构建过程的记事，不是这个包的内容。"""
        self.assertNotIn(".yojigen-caps", self.dump)

    def test_能力位真的设上了(self):
        """★ chown 会清掉 security.capability —— 所以清单要在 chown 之后设。

        判据是把包【解开】再 getcap，不是"我们传了 .yojigen-caps"：传了参数和
        能力位在包里是两件事（这一整段注释就是这么来的）。而如果这个容器里
        装不上 libcap（网络），那 inner.sh 里那一块本来也会静默跳过 —— 那时
        这条测试只能 skip，因为"没设上"和"根本没工具"从外面看不出区别。
        """
        script = ('apk add --no-cache libcap >/dev/null 2>&1;'
                  ' command -v setcap >/dev/null 2>&1 || { echo NO-SETCAP; exit 0; };'
                  ' mkdir -p /x && cd /x && apk extract --allow-untrusted /p.apk'
                  " >/dev/null 2>&1; getcap -r /x 2>/dev/null")
        r = subprocess.run(
            ["docker", "run", "--rm", "-v", f"{self.apk}:/p.apk:ro", "alpine:latest",
             "sh", "-c", script],
            capture_output=True, text=True, timeout=900)

        if "NO-SETCAP" in r.stdout:
            self.skipTest("这个容器里装不上 libcap —— setcap 那段本来就会跳过")
        self.assertIn("cap_net_raw", r.stdout,
                      f"包里的 ping 没有能力位。解开之后的 getcap：\n{r.stdout}{r.stderr}")

    def test_buildkey_旁路文件跟着包走(self):
        dest = self.pkgs / "iputils-20250605-r1.apk.buildkey"
        self.assertTrue(dest.is_file(), "打了包就要留下旁路文件")
        self.assertEqual(dest.read_text(), '{"key":"deadbeef"}')

    def test_同样的输入打两次字节相同(self):
        """★ 可复现：同一个产物、同一个 SOURCE_DATE_EPOCH → 同一个包。

        这条是"mtime 归一化"那句承诺在**字节**上的样子（上面那条看的是
        adbdump 的渲染，这条看的是整个文件）。
        """
        self.assertEqual(self.apk.read_bytes(), self.apk2.read_bytes(),
                         "同一个产物打了两次，字节不同 —— 时间戳没兜住")


if __name__ == "__main__":
    unittest.main()
