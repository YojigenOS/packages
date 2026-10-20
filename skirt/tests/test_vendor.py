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


class Test_force_build_和断环是两件事(unittest.TestCase):
    """§1.5.Bf：`SKIRT_PROVIDES` 只管断环，`SKIRT_REBUILD` 管哪些也要建。

    ★ 这两个集合【必须能分开】，而它们是同一件事的两个方面：

        SKIRT_PROVIDES   "环境里有它，所以【不要等它】"（断边，解决环）
        SKIRT_REBUILD    "而它【也要建】"（所以它进顺序，而指向它的边不断）

    ★ 而 2026-10-20 的第一版修法【直接从 SKIRT_PROVIDES 里减掉】，于是把
      "不要等它"也拿掉了 —— `make → glibc → python3 → zlib → make` 那个环
      立刻露出来，CI 报「依赖成环」。这个文件的那几条测试就是为了让那种
      回归【在本地就挂】，而不是等一轮 CI。
    """

    def setUp(self):
        import os
        self._old = {k: os.environ.get(k) for k in ("SKIRT_PROVIDES", "SKIRT_REBUILD")}

    def tearDown(self):
        import os
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_读环境变量(self):
        import os
        os.environ["SKIRT_REBUILD"] = "readline  ncurses,openssl"
        from skirtlib.deps import load_rebuild_only
        self.assertEqual(load_rebuild_only(), {"readline", "ncurses", "openssl"})

    def test_没设就是空集(self):
        import os
        os.environ.pop("SKIRT_REBUILD", None)
        from skirtlib.deps import load_rebuild_only
        self.assertEqual(load_rebuild_only(), set())

    def test_force_build_里的包_边不断(self):
        """指向 force_build 的边要保留 —— 那样顺序才知道它等谁。

        ★ 而 `force_build` 的语义是【重建名单】，不是"某一个包"。所以给它一个
          孤立的 `{"readline"}` 是【不一致的输入】：真跑的时候 workflow 传的是
          整份交集（`devel` 闭包 ∩ `base`+`minimal` 闭包，49 个），而 `ncurses`
          就在里面。

          第一版这条测试正是传了 `{"readline"}`，于是 `readline → ncurses` 那条
          边还断着 —— 而那不是 bug，是**它给的名单里没有 ncurses**。
          ★ 教训：**孤立元素的输入测不出闭包语义**。
        """
        from skirtlib import order
        # readline 的 build 依赖里有 ncurses；两个都在重建名单里
        graph, broken = order.build_graph(
            "x86_64", {"readline", "ncurses"}, ["readline", "ncurses"],
            force_build={"readline", "ncurses"})
        self.assertIn("ncurses", graph["readline"],
                      "readline 建的时候要等 ncurses —— 两个都在重建名单里，这条边不该断")

    def test_而名单外的依赖_边还是断的(self):
        """★ 这条是【环不回来】的原因：`make` 永远不在重建名单里。

        `ncurses` 的依赖是 gcc / glibc / make —— 三个都是构建工具，base 和
        minimal 里都没有它们，所以它们不在交集里。于是 `ncurses → make` 断，
        而 `make → glibc → python3 → zlib → make` 那个环断在 `make` 上。
        """
        from skirtlib import order
        graph, broken = order.build_graph(
            "x86_64", {"ncurses", "make"}, ["ncurses", "make"],
            force_build={"ncurses"})
        self.assertNotIn("make", graph["ncurses"])
        self.assertIn(("ncurses", "make"), broken)

    def test_而_force_build_外_边还是断的(self):
        """★ 这条是环不回来的原因：`make` 永远不在 rebuild 里。"""
        from skirtlib import order
        # bash 的 build 依赖里有 readline；readline 在 host_provides 而不在 force_build
        graph, broken = order.build_graph(
            "x86_64", {"readline"}, ["bash", "readline"], force_build=set())
        self.assertNotIn("readline", graph["bash"])
        self.assertIn(("bash", "readline"), broken)

    def test_而真实的环_靠一个不在_rebuild_里的包断着(self):
        """`make → glibc → python3 → zlib → make`。

        ★ 这条测的是【设计成立的前提】：那个环上必须至少有一个包【不该重建】
          （用户装不到它），否则它真的成环。而 `make` 正是那一个 —— 它是构建
          工具，base/minimal 里都没有它。

          而这条测试【不跑 CI 那个差集】（那要 distro 仓库），它只断言：
          `make` 不在 base/minimal 的直接列表里。★ 那正是差集把它排除的理由。
        """
        import tomllib
        from pathlib import Path
        根 = Path(__file__).resolve().parents[3]
        for 风味 in ("base", "minimal"):
            f = 根 / "distro" / "variants" / f"{风味}.toml"
            if not f.is_file():
                self.skipTest(f"没有 {f}（这个测试要在工作区里跑）")
            v = tomllib.loads(f.read_text(encoding="utf-8"))["variant"]
            self.assertNotIn("make", v["packages"],
                             f"make 不该在 {风味} 的直接列表里 —— 它是构建工具，"
                             "而它正是那个环的断点")


class Test_runtime_闭包和_build_闭包是两件事(unittest.TestCase):
    """`skirt provides --runtime` —— 回答"用户会装到什么"，不是"环境里有什么"。

    ★ 这条的来历（2026-10-20）：B1 那个修复要算「哪些包也要交付」，第一版用的
      是 `provides` 的默认闭包（runtime + build）。于是 `m4` / `bison` /
      `autoconf` / `perl` 这些【构建工具】被算成"用户会装到的"，进了
      `SKIRT_REBUILD` —— 而 `glibc → bison → m4 → glibc` 那个环就露出来了，
      CI 报「依赖成环」。

      ★ 而那个环【是真的】（glibc 建时要 bison，bison 要 m4，m4 运行时要
        glibc）。错的不是环，是判据。
    """

    def _闭包(self, *args, env=None):
        import os, subprocess
        from pathlib import Path
        环境 = dict(os.environ)
        环境.pop("SKIRT_PROVIDES", None)      # ★ 默认路径会用它断环，见下
        环境.pop("SKIRT_REBUILD", None)
        if env:
            环境.update(env)
        r = subprocess.run([str(Path(__file__).resolve().parent.parent / "skirt"),
                            "provides", *args],
                           capture_output=True, text=True, env=环境)
        self.assertEqual(r.returncode, 0, f"失败: {r.stderr[:200]}")
        return set(r.stdout.split())

    def test_默认路径是要建什么_而不是闭包(self):
        """★★ `provides` 的默认路径走 `build_order` —— 它会【断掉】基线里的包。

        所以它回答的是「要建什么 + 目标」，**不是"闭包"**。第一版测试把两者
        当成一样，于是 `provides bash` 只给了 3 个（bash / ncurses / readline）
        —— 因为 gcc / glibc / make 都在 `bootstrap/provides.txt` 里，被断了。

        ★ 而这个名字（`provides`）容易让人以为是"提供什么"，其实它是
          "构建环境要提供什么、所以哪些不用建"。**命名问题，不是 bug。**
        """
        默认 = self._闭包("bash", env={"SKIRT_PROVIDES": ""})
        不设 = self._闭包("bash")
        self.assertLessEqual(不设, 默认,
                             "设了 SKIRT_PROVIDES 之后只会更少（那些包被断了）")

    def test_runtime_闭包不带构建工具(self):
        """★ 这是那 19 个差集的核心：构建工具不该出现在 runtime 闭包里。"""
        r = self._闭包("--runtime", "bash")
        # bash 的 runtime 依赖是 glibc + readline（而 readline 要 ncurses）
        for 不该 in ("gcc", "make", "m4", "bison", "autoconf"):
            self.assertNotIn(不该, r, f"{不该} 是构建工具，不该在 runtime 闭包里")
        self.assertIn("glibc", r)
        self.assertIn("readline", r, "bash 运行时要 readline")

    def test_runtime_闭包_子包目标也能算(self):
        """★ `glibc-locales` / `dash-binsh` 是【子包】。

        第一版只映射了依赖那一侧，于是 minimal 的 46 个目标里有 2 个直接报
        「找不到配方: glibc-locales」。**而这个项目已经为子包吃过一次亏**
        （provides 里写母包名，于是 openssh 被报缺 libcrypto）。
        """
        r = self._闭包("--runtime", "glibc-locales")
        self.assertIn("glibc", r, "glibc-locales 该映射回 glibc")

    def test_runtime_闭包和默认路径_是两个不同的东西(self):
        """★ 别拿 `provides` 的默认输出当"闭包"来比 —— 它两条路径的语义不同：

            默认        build_order 的结果（断过环）
            --runtime   纯 runtime 依赖闭包（不断环，也不看 SKIRT_PROVIDES）

        而 `--runtime` 【不该】看 `SKIRT_PROVIDES`：它回答的是"用户会装到什么"，
        那和环境里有什么无关。**那正是它和默认路径该分开的理由。**
        """
        # 同一个包，设了 SKIRT_PROVIDES 时 --runtime 的结果【不变】
        a = self._闭包("--runtime", "bash")
        b = self._闭包("--runtime", "bash", env={"SKIRT_PROVIDES": "glibc"})
        self.assertEqual(a, b,
                         "--runtime 不看 SKIRT_PROVIDES（用户装什么和环境无关）")
