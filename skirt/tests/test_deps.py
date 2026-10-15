#!/usr/bin/env python3
"""skirt 的单元测试。

跑法:
    python3 -m unittest discover -s skirt/tests -v

这些测试不需要 Docker，也不构建任何东西——只覆盖纯逻辑。重点在
deps.dep_closure()：那里编码的是自举语义，是 skirt 里最容易出错、
也最不该出错的部分。三条规则都是踩坑踩出来的：

  1. 构建环境基线（bootstrap/provides.txt）里的包，跳过
  2. 自己依赖自己 = 用上一轮构建产物，**不是**成环
  3. 真正的环（glibc → make → glibc）必须报错

哪天重构时改错一条，表现会是「某个包构建出奇怪的结果」而不是报错。
所以它们值得有测试守着。
"""

from __future__ import annotations

import datetime
import re
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from skirtlib import build as build_mod
from skirtlib import collect as collect_mod
from skirtlib import spec as spec_mod
from skirtlib import deps as deps_mod
from skirtlib.build import run_in_container
from skirtlib.common import RECIPES_DIR, build_jobs

#: 依赖闭包测试要用的基线。直接用真实的 bootstrap/provides.txt——不要在这里
#: 另写一份：【另写一份就会漂移】，而它漂移的表现是测试莫名其妙地报环，
#: 或者更糟：报一个已经不存在的环。工具链和 autotools 都在里面，原因见
#: 那个文件自己的注释（它们都是「构建构建系统」的工具，靠排序解不开）。
from skirtlib.deps import load_host_provides as _load_host_provides
TOOLCHAIN_BASELINE = _load_host_provides()
from skirtlib.deps import load_host_provides, merge_tree, source_date_epoch
from skirtlib.spec import (deps_for, direct_deps, expand, is_subpackage,
                           latest_date, load_spec, package_date, recipe_of,
                           subpackages)

from support import _dt, _pkg, _all_spec_dirs

class TestDepsFor(unittest.TestCase):
    """★ 两个架构各写一份、**没有「通用」**（2026-10-15 改的语义）。

    这一组测试原来守的是旧语义 ——「通用 + 架构整键覆盖」。那个写法有两个洞，
    而两个都在真实配方里发作了：

    **① 覆盖是整键的** → 某个架构段只写一半就会让另一半"看起来是空的"。
       有一条测试甚至把这个叫 `test_arch_overrides_same_key_wholesale`，
       也就是把那个行为当成正确的写进去了。

    **② 另一个架构那份会漏进结果** → 有一条测试叫
       `test_other_arch_key_survives`，docstring 写的是：

           「已知的粗糙之处……把它钉成测试，是为了将来真要清理时能看见影响面。」

       ——而这次就是那次清理。**它现在反过来断言：不许漏出来。**
    """

    def _spec(self, deps):
        return {"package": {"name": "x", "version": "1"}, "dependencies": deps}

    def test_两个架构各给各的(self):
        s = self._spec({"aarch64": {"runtime": ["glibc"], "build": ["make"]},
                        "x86_64":  {"runtime": ["glibc"], "build": ["make", "gcc"]}})
        self.assertEqual(deps_for(s, "aarch64"),
                         {"runtime": ["glibc"], "build": ["make"]})
        self.assertEqual(deps_for(s, "x86_64"),
                         {"runtime": ["glibc"], "build": ["make", "gcc"]})

    def test_结果里不许带别的架构键(self):
        """★ 这就是原来那条 `test_other_arch_key_survives` 的反面。"""
        s = self._spec({"aarch64": {"runtime": ["glibc"]},
                        "x86_64":  {"runtime": ["glibc"], "build": ["gcc"]}})
        结果 = deps_for(s, "aarch64")
        self.assertNotIn("x86_64", 结果)
        self.assertNotIn("aarch64", 结果)
        self.assertEqual(结果, {"runtime": ["glibc"]})

    def test_通用段会被明确拒绝(self):
        """★ 不是"忽略"，是【报错】。

        留一段没人读的依赖，正是「改了不生效」最容易生根的地方 ——
        而 try 那样正是这个项目最怕的形状。所以让它响亮地失败。
        """
        s = self._spec({"runtime": ["glibc"], "build": ["make"],
                        "x86_64": {"runtime": ["glibc"], "build": ["make"]}})
        with self.assertRaises(ValueError) as cm:
            deps_for(s, "aarch64")
        self.assertIn("通用", str(cm.exception))

    def test_缺了某一个架构也要报错(self):
        s = self._spec({"aarch64": {"runtime": ["glibc"]}})
        with self.assertRaises(ValueError) as cm:
            deps_for(s, "x86_64")
        self.assertIn("x86_64", str(cm.exception))

    def test_没有依赖段也报错(self):
        """每个包至少要有 glibc，所以"没有依赖段"一定是漏写了。"""
        with self.assertRaises(ValueError):
            deps_for({"package": {"name": "x", "version": "1"}}, "aarch64")

    def test_返回的是副本(self):
        """改了返回的东西不该动到 spec。"""
        s = self._spec({"aarch64": {"runtime": ["glibc"]},
                        "x86_64":  {"runtime": ["glibc"]}})
        结果 = deps_for(s, "aarch64")
        结果["runtime"].append("改坏了")
        self.assertEqual(deps_for(s, "aarch64")["runtime"], ["glibc"])


class Test所有配方都守这个约定(unittest.TestCase):
    """★ 而这一条是真正会守住它的那个 —— 上面那些用的是手搓的字典。"""

    def test_每个配方都是两个架构各一份(self):
        import pathlib as _p
        import tomllib
        from skirtlib.common import RECIPES_DIR
        坏 = []
        for sp in sorted(_p.Path(RECIPES_DIR).rglob("spec.toml")):
            d = tomllib.loads(sp.read_text())
            keys = set(d.get("dependencies") or {})
            if keys != {"aarch64", "x86_64"}:
                坏.append(f"{sp.parent.name}: {sorted(keys)}")
        self.assertFalse(坏, "这些配方的依赖段不是「两个架构各一份」：\n  " + "\n  ".join(坏))

    def test_每个配方两个架构都解析得出来(self):
        """上面那条看的是【键名】，这一条真的调一次解析 —— 两条都要。

        （"文件里有那个段"和"解析得出来"是两件事，这一整批就是被那条差别
          绊过一次：`zlib` 的 x86_64 段看起来少了 `build`，而解析出来是有的。）
        """
        import pathlib as _p
        import tomllib
        from skirtlib.common import RECIPES_DIR
        坏 = []
        for sp in sorted(_p.Path(RECIPES_DIR).rglob("spec.toml")):
            d = tomllib.loads(sp.read_text())
            for arch in ("aarch64", "x86_64"):
                try:
                    deps_for(d, arch)
                except ValueError as e:
                    坏.append(f"{sp.parent.name} / {arch}: {e}")
        self.assertFalse(坏, "这些配方解析不出来：\n  " + "\n  ".join(坏))


class TestDirectDeps(unittest.TestCase):
    """直接依赖 = runtime + build，去重且保持声明顺序。"""

    def test_runtime_comes_before_build(self):
        s = {"package": {"name": "x", "version": "1"},
             "dependencies": {"aarch64": {"build": ["make"], "runtime": ["glibc"]}, "x86_64": {"build": ["make"], "runtime": ["glibc"]}}}
        self.assertEqual(direct_deps(s, "aarch64"), ["glibc", "make"])

    def test_dedup_preserves_order(self):
        s = {"package": {"name": "x", "version": "1"},
             "dependencies": {"aarch64": {"runtime": ["glibc", "zlib"],
                              "build": ["make", "zlib"]}, "x86_64": {"runtime": ["glibc", "zlib"],
                              "build": ["make", "zlib"]}}}
        self.assertEqual(direct_deps(s, "aarch64"), ["glibc", "zlib", "make"])

    def test_两个架构都空就是空(self):
        """依赖段【必须】在，而两个架构的列表都可以是空的（比如 filesystem）。"""
        s = {"package": {"name": "x", "version": "1"},
             "dependencies": {"aarch64": {"runtime": [], "build": []},
                              "x86_64":  {"runtime": [], "build": []}}}
        self.assertEqual(direct_deps(s, "aarch64"), [])

class TestDepClosure(unittest.TestCase):
    """依赖闭包——自举语义所在。"""

    def test_host_provides_are_skipped(self):
        s = load_spec("hello")
        self.assertNotIn("glibc", deps_mod.dep_closure(s, "aarch64", TOOLCHAIN_BASELINE))

    def test_self_dependency_uses_previous_build(self):
        """make 依赖 make。已构建过时，应把上一轮的自己排进依赖链——
        这正是「用自建的 make 重建 make」能成立的原因。"""
        s = load_spec("make")
        if deps_mod.built_payload("make", "aarch64") is None:
            self.skipTest("make 还没构建过，无法验证自依赖")
        self.assertEqual(deps_mod.dep_closure(s, "aarch64", TOOLCHAIN_BASELINE), ["make"])

    def test_self_dependency_skipped_when_never_built(self):
        """从没构建过时，自依赖应当被跳过（交给构建环境），而不是报错。"""
        s = {"package": {"name": "没有配方的包", "version": "1"},
             "dependencies": {"aarch64": {"build": ["没有配方的包"]}, "x86_64": {"build": ["没有配方的包"]}}}
        self.assertEqual(deps_mod.dep_closure(s, "aarch64", set()), [])

    def test_real_cycle_is_reported(self):
        """glibc 构建需要 make，make 运行需要 glibc。基线里没有 glibc 时，
        必须被报成环——而不是静默递归或给出错误顺序。"""
        s = load_spec("glibc")
        with self.assertRaises(SystemExit):
            deps_mod.dep_closure(s, "aarch64", set())

    def test_unknown_dependency_exits(self):
        s = {"package": {"name": "x", "version": "1"},
             "dependencies": {"aarch64": {"runtime": ["根本没有这个包"]}, "x86_64": {"runtime": ["根本没有这个包"]}}}
        with self.assertRaises(SystemExit):
            deps_mod.dep_closure(s, "aarch64", set())

    def test_closure_is_topologically_ordered(self):
        """闭包里任何一个包，它的依赖要么在基线里，要么排在它前面。"""
        order = deps_mod.dep_closure(load_spec("glibc"), "aarch64", TOOLCHAIN_BASELINE)
        self.assertTrue(order, "glibc 的闭包不该是空的")
        for name in order:
            for dep in direct_deps(load_spec(name), "aarch64"):
                if dep == name or dep not in order:
                    continue
                with self.subTest(package=name, dependency=dep):
                    self.assertLess(order.index(dep), order.index(name),
                                    f"{dep} 应该排在 {name} 前面")

class TestMergeTree(unittest.TestCase):
    """依赖合并进 sysroot。

    这里原本用 shutil.copytree，遇到「目标已有同名符号链接」会抛
    File exists——而 gcc 和 filesystem 都提供 usr/lib64 → lib，正好撞上，
    导致 glibc 的一次构建失败。以下用例是那次故障的回归测试。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.src, self.dst = root / "src", root / "dst"
        self.src.mkdir()
        self.dst.mkdir()

    def _file(self, base, rel, text):
        p = base / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        return p

    def _link(self, base, rel, target):
        p = base / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.symlink_to(target)
        return p

    def test_symlink_over_same_symlink(self):
        """回归：两个包提供同一个符号链接（gcc 与 filesystem 的 usr/lib64）。"""
        self._link(self.src, "usr/lib64", "lib")
        self._link(self.dst, "usr/lib64", "lib")
        merge_tree(self.src, self.dst)          # 不该抛异常
        self.assertTrue((self.dst / "usr/lib64").is_symlink())
        self.assertEqual(os.readlink(self.dst / "usr/lib64"), "lib")

    def test_symlink_overwrites_file(self):
        self._link(self.src, "x", "target")
        self._file(self.dst, "x", "我是文件")
        merge_tree(self.src, self.dst)
        self.assertTrue((self.dst / "x").is_symlink())

    def test_file_overwrites_symlink(self):
        """这条最阴：shutil.copy2 遇到符号链接会跟着它跑到链接指向的地方写，
        而不是替换链接本身。"""
        self._file(self.src, "x", "我是新文件")
        self._link(self.dst, "x", "某个地方")
        merge_tree(self.src, self.dst)
        self.assertFalse((self.dst / "x").is_symlink())
        self.assertEqual((self.dst / "x").read_text(), "我是新文件")

    def test_file_overwrites_file(self):
        self._file(self.src, "x", "新")
        self._file(self.dst, "x", "旧")
        merge_tree(self.src, self.dst)
        self.assertEqual((self.dst / "x").read_text(), "新")

    def test_symlink_overwrites_directory(self):
        self._link(self.src, "x", "target")
        self._file(self.dst, "x/inner.txt", "内容")
        merge_tree(self.src, self.dst)
        self.assertTrue((self.dst / "x").is_symlink())

    def test_directories_merge_rather_than_replace(self):
        """两边都有同一个目录时，应该合并内容，而不是覆盖掉一边。"""
        self._file(self.src, "d/a.txt", "a")
        self._file(self.dst, "d/b.txt", "b")
        merge_tree(self.src, self.dst)
        self.assertEqual((self.dst / "d/a.txt").read_text(), "a")
        self.assertEqual((self.dst / "d/b.txt").read_text(), "b")

    def test_nested(self):
        self._file(self.src, "a/b/c.txt", "你好")
        merge_tree(self.src, self.dst)
        self.assertEqual((self.dst / "a/b/c.txt").read_text(), "你好")

    def test_directory_modes_preserved(self):
        """回归：/tmp 是 1777、/root 是 0700。

        merge_tree 用 mkdir 建目录会丢掉这些模式，容器里 cp -a 再把错的
        权限铺到 /tmp 上，降权执行的构建就写不进去——报的是
        "cannot create a temporary directory in /tmp"，看不出和权限有关。"""
        import stat as stat_mod
        for rel, mode in (("tmp", 0o1777), ("root", 0o700)):
            d = self.src / rel
            d.mkdir()
            d.chmod(mode)
        merge_tree(self.src, self.dst)
        for rel, mode in (("tmp", 0o1777), ("root", 0o700)):
            with self.subTest(dir=rel):
                actual = stat_mod.S_IMODE((self.dst / rel).stat().st_mode)
                self.assertEqual(actual, mode, f"/{rel} 的权限应该是 {oct(mode)}")

class TestPackageDate(unittest.TestCase):
    """配方日期 —— SOURCE_DATE_EPOCH 的来源。

    它必须**写在配方里**，不能从 git 提交时间或文件 mtime 推导：那些是
    「关于内容的元数据」，改动提交之前指向旧值、提交之后才变成新值，于是
    同一份配方在本地和 CI 上会算出不同的时间戳。写进文件，它就随同一次
    提交一起生效，提交前后一致。
    """

    def test_utc(self):
        self.assertEqual(package_date(_pkg("x", _dt("2026-10-10T15:30:00Z"))),
                         1791646200)

    def test_offset_is_the_same_instant(self):
        """北京时间和 UTC 指向同一时刻，必须算出同一个值。"""
        utc = _pkg("x", _dt("2026-10-10T15:30:00Z"))
        bj = _pkg("x", _dt("2026-10-10T23:30:00+08:00"))
        self.assertEqual(package_date(utc), package_date(bj))

    def test_missing_date_dies(self):
        with self.assertRaises(SystemExit):
            package_date({"package": {"name": "x", "version": "1", "release": 1}})

    def test_quoted_string_dies(self):
        """加了引号就是普通字符串，不是时间——很容易犯，必须报错。"""
        with self.assertRaises(SystemExit):
            package_date(_pkg("x", "2026-10-10T15:30:00Z"))

    def test_naive_datetime_dies(self):
        """不带时区的话，同一时刻在不同时区会算出不同时间戳。"""
        with self.assertRaises(SystemExit):
            package_date(_pkg("x", datetime.datetime(2026, 10, 10, 15, 30)))

class TestLatestDate(unittest.TestCase):
    """SOURCE_DATE_EPOCH 吃整个依赖链，而不是只看自己。

    理由：配方一个字没改、但依赖更新了，产出的字节**确实**会变——链接进去
    的库换了。时间戳要是纹丝不动，就会产出「同名、同 mtime、内容不同」的包，
    而那正是所有靠 mtime 判断变化的同步与备份工具的杀手。Debian 到今天还在
    靠人在 binNMU 时手工把时间戳加一来绕开它。
    """

    def test_own_date_wins_when_deps_are_older(self):
        own = _pkg("app", _dt("2026-06-01T00:00:00Z"))
        deps = [_pkg("lib", _dt("2026-01-01T00:00:00Z"))]
        self.assertEqual(latest_date(own, deps), package_date(own))

    def test_dependency_bump_propagates(self):
        """核心行为：依赖的日期变晚，本包跟着变晚。"""
        own = _pkg("app", _dt("2026-01-01T00:00:00Z"))
        before = latest_date(own, [_pkg("lib", _dt("2026-01-01T00:00:00Z"))])
        after = latest_date(own, [_pkg("lib", _dt("2026-06-01T00:00:00Z"))])
        self.assertLess(before, after)

    def test_takes_the_latest_of_many(self):
        own = _pkg("app", _dt("2026-01-01T00:00:00Z"))
        deps = [_pkg("a", _dt("2026-03-01T00:00:00Z")),
                _pkg("b", _dt("2026-09-01T00:00:00Z")),   # 最晚
                _pkg("c", _dt("2026-05-01T00:00:00Z"))]
        self.assertEqual(latest_date(own, deps), package_date(deps[1]))

    def test_no_deps(self):
        own = _pkg("app", _dt("2026-06-01T00:00:00Z"))
        self.assertEqual(latest_date(own, []), package_date(own))
