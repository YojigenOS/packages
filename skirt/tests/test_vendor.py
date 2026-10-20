"""决策记录 0022 的落地：`vendor` 记号 + 分发包的库依赖检查（5.12.1 / 5.12.2）。

★ 为什么这个文件存在，而它测的东西【现在一个都还没有】：

  0022 定了「有能直接运行的官方二进制就只分发，不编译」，而它的效果是 rust /
  go / node / bun / docker / containerd / runc / 1panel 全部落进「分发」。
  而【现在一个分发包都还没写】—— 89 个配方全是自己建的。

  所以这两条检查在真的跑到之前，会【安静地过】。而「安静地过」正是这个项目
  一直在修的那一类：一个该说话的地方不说话。

  于是把它拆成能测的两半：

      ① `vendor_risky_needs`  —— 纯函数，喂假的 meta / payload 就能测
      ② `BUILD_KEYS` 那个白名单 —— 喂一个写错键的 spec 就能测

  而这是这个项目的老办法：把判定从「跑一次真的构建」里拆出来。
"""

import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from skirtlib import pack  # noqa: E402
from skirtlib import spec  # noqa: E402


def 假扫描(needed):
    """造一个 scanner：不读真文件，直接吐一组 soname。"""
    return lambda payload: (list(needed), {})


class Test分发包的库依赖检查(unittest.TestCase):
    """5.12.2：分发包链了哪些"我们可能没有"的库。"""

    def test_自己建的包不查(self):
        """返回 None，不是 []。**这个区别是重点。**"""
        with TemporaryDirectory() as d:
            r = pack.vendor_risky_needs({}, Path(d), [])
        self.assertIsNone(r, "自己建的包该返回 None（= 这一格不适用）")

    def test_分发包而一个库都没链(self):
        """返回 []。而它不是 None —— 那表示"查了，干净"。"""
        with TemporaryDirectory() as d:
            r = pack.vendor_risky_needs(
                {"build": {"vendor": "https://example.invalid/x"}},
                Path(d), [], scanner=假扫描([]))
        self.assertEqual(r, [])

    def test_链了而自己没声明的会被报出来(self):
        with TemporaryDirectory() as d:
            r = pack.vendor_risky_needs(
                {"build": {"vendor": "x"}}, Path(d), [],
                scanner=假扫描(["libfoo.so.2", "libbar.so.1"]))
        self.assertEqual(sorted(r), ["libbar.so.1", "libfoo.so.2"])

    def test_自己声明过的不报(self):
        with TemporaryDirectory() as d:
            r = pack.vendor_risky_needs(
                {"build": {"vendor": "x"}}, Path(d), ["so:libfoo.so.2"],
                scanner=假扫描(["libfoo.so.2"]))
        self.assertEqual(r, [], "声明了的（so: 前缀）不该报")

    def test_写成包名声明也算声明过(self):
        """`depends = ["openssl"]` 这种写法 —— 那不是 soname，但作者想过。"""
        with TemporaryDirectory() as d:
            r = pack.vendor_risky_needs(
                {"build": {"vendor": "x"}}, Path(d), ["openssl"],
                scanner=假扫描(["openssl"]))
        self.assertEqual(r, [])

    def test_glibc_提供的不报(self):
        """那一份硬编码清单里的，谁的系统上都有。"""
        with TemporaryDirectory() as d:
            r = pack.vendor_risky_needs(
                {"build": {"vendor": "x"}}, Path(d), [],
                scanner=假扫描(sorted(pack.GLIBC_SONAMES)))
        self.assertEqual(r, [], "GLIBC_SONAMES 里的一个都不该报")

    def test_而清单外的会被报(self):
        """★ 反面：如果那条"跳过 glibc"的判断写坏了（比如永远跳过），
        这个测试会挂。**一个只测"不该报"的测试不证明它真的会报。**"""
        with TemporaryDirectory() as d:
            r = pack.vendor_risky_needs(
                {"build": {"vendor": "x"}}, Path(d), [],
                scanner=假扫描(["libc.so.6", "libnss3.so"]))
        self.assertEqual(r, ["libnss3.so"])


class TestBuild段的键白名单(unittest.TestCase):
    """5.12.1 顺带：`[build]` 多写一个键要报错，不能静静忽略。"""

    def test_vendor_在白名单里(self):
        self.assertIn("vendor", spec.BUILD_KEYS)

    def test_白名单和_build_读的键一致(self):
        """★ 这条守的是【两处必须同步】这件事 —— 而它没有自动保证。

        白名单在 `spec.py`，真正读键在 `build.py`。有人给 build.py 加一个新
        键而忘了白名单，那个键会被【立刻拒绝】，而报错信息里列着"认识的只有"
        ——于是他会去改白名单。反过来：白名单里有而 build.py 不读的键，
        那是【谎报】，而这条测试能抓到。
        """
        import re
        源 = (Path(__file__).resolve().parent.parent / "skirtlib" / "build.py").read_text()
        读的 = set(re.findall(r'b\.get\("([a-z_]+)"', 源))
        # nostrip 是 b.get("nostrip") 之外还有别的写法，先看差集
        白名单有而没有读 = spec.BUILD_KEYS - 读的 - {"nostrip"}
        # 这两个【不影响构建】，只影响别处的检查，所以不算"该被 build.py 读"。
        #
        #   vendor        pack.py 读它（决定要不要报库依赖，5.12.2）
        #   vendor_check  bootstrap.yml 的自检读它（决定跑什么命令，5.12.3）
        #
        # ★ 而它们是【加进白名单的一部分】—— 白名单管的是"配方里允许写什么"，
        #   不是"build.py 会读什么"。两者恰好大部分重叠，而这四个键就是例外。
        白名单有而没有读 -= {"vendor", "vendor_check"}
        self.assertEqual(
            白名单有而没有读, set(),
            f"这些键在白名单里，而 build.py 从不读它们（写了会被静默忽略）：{白名单有而没有读}")

    def test_而写的键会被真的读(self):
        """★ 反面：build.py 读的键都得在白名单里，否则配方会被误拒。"""
        import re
        源 = (Path(__file__).resolve().parent.parent / "skirtlib" / "build.py").read_text()
        读的 = set(re.findall(r'b\.get\("([a-z_]+)"', 源))
        self.assertEqual(读的 - spec.BUILD_KEYS, set())


if __name__ == "__main__":
    unittest.main()
