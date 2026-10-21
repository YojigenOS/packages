"""增量：`cmd_build` 在 build_key 没变时【跳过】。

★★★ 为什么这个文件存在（2026-10-21）：

在那之前，`build_key` / `.buildkey` / `skirt stale` 那一整套只用来**报告** ——
`cmd_build` 从来不问它，`cmd_bootstrap` 也就无条件全量。于是每一轮 bootstrap
都是 90 个包 × 2 架构 × 约 2 小时，而其中绝大多数这一轮一个字都没变。

实测过：同一份 main、同一份镜像连跑两轮，第二轮照样重建全部。

★ 而那一刻人会去查"key 里哪一样变了"（我今天就查了，还改了两处成分），
  而真相是**没人问它**。所以这里要把"问"这件事钉住。
"""

import os
import sys
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from skirtlib import buildkey  # noqa: E402
from skirtlib import commands  # noqa: E402
from skirtlib import deps  # noqa: E402
from skirtlib.spec import load_spec  # noqa: E402


class Test_key_没变就跳过(unittest.TestCase):
    def _准备好(self, tmp: Path, *, 指纹: str):
        """造一个"盘上已经有这份产物、而 key 和现在算的一样"的局面。"""
        spec = load_spec("hello")
        name = spec["package"]["name"]
        host = deps.load_host_provides()
        epoch = deps.source_date_epoch(spec, "x86_64", host)
        bk = buildkey.compute(spec, "x86_64", epoch,
                              [(d, "k") for d in
                               deps.dep_closure(spec, "x86_64", host,
                                               include_rebuild=True)])
        out, rel = deps.output_dir(name, "x86_64")
        (out / "payload").mkdir(parents=True, exist_ok=True)
        (out / "payload" / "usr-bin-hello").write_text("x\n", encoding="utf-8")
        (out / "meta.json").write_text(
            json.dumps({"build_key_parts": bk["parts"]}, ensure_ascii=False),
            encoding="utf-8")
        return spec, out

    def _跑(self, tmp: Path, *, 记录指纹: str, 跑指纹: str, 有payload=True):
        """造一份"盘上已有"的产物，再用【另一个】指纹跑一次。

        ★ `记录指纹` 和 `跑指纹` 要分开 —— 第一版在 `_跑` 里又准备了一次，
          于是"盘上那份"每次都被覆盖成当前指纹的，`test_指纹变了就重建` 就
          永远看到"没变"。**那是夹具自己把被测的东西抹掉了。**
        """
        调用了容器 = []
        输出 = []

        def 假的容器(*a, **kw):
            调用了容器.append(1)
            raise AssertionError("去建了")

        # ★ 指纹有【进程内缓存】（一个进程只问一次容器）—— 所以测试里换指纹
        #   必须先清它，否则第二次拿到的还是第一次的值。生产里一个进程只有
        #   一个指纹，那条缓存是对的。
        buildkey._TOOLCHAIN_CACHE.clear()
        with mock.patch.dict(os.environ, {"SKIRT_TOOLCHAIN": 记录指纹,
                                          "SKIRT_PROVIDES": ""}), \
             mock.patch.object(deps, "OUTPUT_DIR", tmp / "output"):
            spec, out = self._准备好(tmp, 指纹=记录指纹)
        buildkey._TOOLCHAIN_CACHE.clear()
        if not 有payload:
            import shutil
            shutil.rmtree(out / "payload")
            (out / "payload").mkdir()

        with mock.patch.dict(os.environ, {"SKIRT_TOOLCHAIN": 跑指纹,
                                          "SKIRT_PROVIDES": ""}), \
             mock.patch.object(deps, "OUTPUT_DIR", tmp / "output"), \
             mock.patch.object(commands, "run_in_container", 假的容器), \
             mock.patch.object(commands, "build_sysroot", lambda *a, **k: None), \
             mock.patch("builtins.print", lambda *a, **k: 输出.append(" ".join(map(str, a)))):
            try:
                commands.cmd_build(SimpleNamespace(package="hello", arch="x86_64"))
            except AssertionError:
                return False, 输出
        return not 调用了容器, 输出

    def test_key_没变就跳过(self):
        with TemporaryDirectory() as d:
            跳过, 输出 = self._跑(Path(d), 记录指纹="固定指纹", 跑指纹="固定指纹")
        self.assertTrue(跳过, "key 和盘上那份一样，就不该去建")
        self.assertTrue(any("跳过" in l for l in 输出),
                        "而它得【说出来】—— 一声不响地跳过和一声不响地重建一样糟")

    def test_指纹变了就重建(self):
        """★ 反面：这条要能失败，否则上一条只是"永远跳过"。"""
        with TemporaryDirectory() as d:
            跳过, 输出 = self._跑(Path(d), 记录指纹="指纹A", 跑指纹="指纹B")
        self.assertFalse(跳过, "指纹变了必须重建（产物可能不同）")
        self.assertTrue(any("toolchain" in l for l in 输出),
                        f"要说出是哪一个成分变了，而它说：{输出[-6:]}")

    def test_只有_key_相同而_payload_空了也要重建(self):
        """★★ 跳过的前提是盘上真有一份可用的产物。

        光"key 相同"不够 —— 一个空目录会让它悄悄产出一个空包，而那正是
        `check_dest` 当初为它加的那类事故。
        """
        with TemporaryDirectory() as d:
            跳过, 输出 = self._跑(Path(d), 记录指纹="固定指纹", 跑指纹="固定指纹", 有payload=False)
        self.assertFalse(跳过, "payload 空着就必须重建，哪怕 key 一样")
        self.assertTrue(any("payload" in l for l in 输出), f"要说清原因：{输出[-6:]}")
