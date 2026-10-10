#!/usr/bin/env python3
"""skirt 的单元测试。

跑法:
    python3 -m unittest discover -s tools/tests -v

这些测试不需要 Docker，也不构建任何东西——只覆盖纯逻辑。重点在
deps.dep_closure()：那里编码的是自举语义，是 skirt 里最容易出错、
也最不该出错的部分。三条规则都是踩坑踩出来的：

  1. 构建环境基线（host/provides.txt）里的包，跳过
  2. 自己依赖自己 = 用上一轮构建产物，**不是**成环
  3. 真正的环（glibc → make → glibc）必须报错

哪天重构时改错一条，表现会是「某个包构建出奇怪的结果」而不是报错。
所以它们值得有测试守着。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from skirtlib import deps as deps_mod
from skirtlib.common import PACKAGES_DIR
from skirtlib.spec import deps_for, direct_deps, expand, load_spec


class TestExpand(unittest.TestCase):
    """$NAME / $VERSION 占位符展开。"""

    SPEC = {"package": {"name": "foo", "version": "1.2.3"}}

    def test_string(self):
        self.assertEqual(
            expand(self.SPEC, "https://x/$NAME/$NAME-$VERSION.tar.xz"),
            "https://x/foo/foo-1.2.3.tar.xz")

    def test_list(self):
        self.assertEqual(
            expand(self.SPEC, ["$NAME", "--version=$VERSION"]),
            ["foo", "--version=1.2.3"])

    def test_dict(self):
        self.assertEqual(
            expand(self.SPEC, {"a": "$NAME", "b": ["$VERSION"]}),
            {"a": "foo", "b": ["1.2.3"]})

    def test_non_string_passthrough(self):
        for value in (42, None, True, 3.14):
            with self.subTest(value=value):
                self.assertEqual(expand(self.SPEC, value), value)

    def test_unknown_placeholder_left_alone(self):
        """只有 $NAME 和 $VERSION 会被展开，别的原样保留。"""
        self.assertEqual(expand(self.SPEC, "$HOME/$NAME"), "$HOME/foo")


class TestDepsFor(unittest.TestCase):
    """通用依赖与架构特定依赖的合并。"""

    def test_base_only(self):
        s = {"package": {"name": "x", "version": "1"},
             "dependencies": {"runtime": ["glibc"], "build": ["make"]}}
        self.assertEqual(deps_for(s, "aarch64"),
                         {"runtime": ["glibc"], "build": ["make"]})

    def test_no_dependencies_section(self):
        self.assertEqual(deps_for({"package": {"name": "x", "version": "1"}}, "aarch64"), {})

    def test_arch_overrides_same_key_wholesale(self):
        """架构段对同名键是整体覆盖，不是追加。"""
        s = {"package": {"name": "x", "version": "1"},
             "dependencies": {"runtime": ["glibc"], "build": ["make"],
                              "x86_64": {"build": ["make", "gcc"]}}}
        d = deps_for(s, "x86_64")
        self.assertEqual(d["build"], ["make", "gcc"])
        self.assertEqual(d["runtime"], ["glibc"])

    def test_requested_arch_key_removed(self):
        """架构段本身不该作为一个依赖字段漏出来。"""
        s = {"package": {"name": "x", "version": "1"},
             "dependencies": {"runtime": [], "x86_64": {"runtime": ["glibc"]}}}
        self.assertNotIn("x86_64", deps_for(s, "x86_64"))

    def test_other_arch_key_survives(self):
        """已知的粗糙之处：解析 aarch64 时，x86_64 段仍留在结果里。

        direct_deps() 只看 runtime / build，所以目前无害。把它钉成测试，
        是为了将来真要清理时能看见影响面。"""
        s = {"package": {"name": "x", "version": "1"},
             "dependencies": {"x86_64": {"build": ["gcc"]}}}
        self.assertIn("x86_64", deps_for(s, "aarch64"))


class TestDirectDeps(unittest.TestCase):
    """直接依赖 = runtime + build，去重且保持声明顺序。"""

    def test_runtime_comes_before_build(self):
        s = {"package": {"name": "x", "version": "1"},
             "dependencies": {"build": ["make"], "runtime": ["glibc"]}}
        self.assertEqual(direct_deps(s, "aarch64"), ["glibc", "make"])

    def test_dedup_preserves_order(self):
        s = {"package": {"name": "x", "version": "1"},
             "dependencies": {"runtime": ["glibc", "zlib"],
                              "build": ["make", "zlib"]}}
        self.assertEqual(direct_deps(s, "aarch64"), ["glibc", "zlib", "make"])

    def test_empty(self):
        s = {"package": {"name": "x", "version": "1"}}
        self.assertEqual(direct_deps(s, "aarch64"), [])


class TestLoadSpec(unittest.TestCase):
    def test_missing_package_exits(self):
        with self.assertRaises(SystemExit):
            load_spec("这个包根本不存在")

    def test_real_package(self):
        s = load_spec("zlib")
        self.assertEqual(s["package"]["name"], "zlib")
        self.assertIn("_dir", s, "load_spec 应该把配方目录放进 _dir")


class TestDepClosure(unittest.TestCase):
    """依赖闭包——自举语义所在。"""

    def test_host_provides_are_skipped(self):
        s = load_spec("hello")
        self.assertNotIn("glibc", deps_mod.dep_closure(s, "aarch64", {"glibc"}))

    def test_self_dependency_uses_previous_build(self):
        """make 依赖 make。已构建过时，应把上一轮的自己排进依赖链——
        这正是「用自建的 make 重建 make」能成立的原因。"""
        s = load_spec("make")
        if deps_mod.built_payload("make", "aarch64") is None:
            self.skipTest("make 还没构建过，无法验证自依赖")
        self.assertEqual(deps_mod.dep_closure(s, "aarch64", {"glibc"}), ["make"])

    def test_self_dependency_skipped_when_never_built(self):
        """从没构建过时，自依赖应当被跳过（交给构建环境），而不是报错。"""
        s = {"package": {"name": "没有配方的包", "version": "1"},
             "dependencies": {"build": ["没有配方的包"]}}
        self.assertEqual(deps_mod.dep_closure(s, "aarch64", set()), [])

    def test_real_cycle_is_reported(self):
        """glibc 构建需要 make，make 运行需要 glibc。基线里没有 glibc 时，
        必须被报成环——而不是静默递归或给出错误顺序。"""
        s = load_spec("glibc")
        with self.assertRaises(SystemExit):
            deps_mod.dep_closure(s, "aarch64", set())

    def test_unknown_dependency_exits(self):
        s = {"package": {"name": "x", "version": "1"},
             "dependencies": {"runtime": ["根本没有这个包"]}}
        with self.assertRaises(SystemExit):
            deps_mod.dep_closure(s, "aarch64", set())

    def test_closure_is_topologically_ordered(self):
        """闭包里任何一个包，它的依赖要么在基线里，要么排在它前面。"""
        order = deps_mod.dep_closure(load_spec("glibc"), "aarch64", {"glibc"})
        self.assertTrue(order, "glibc 的闭包不该是空的")
        for name in order:
            for dep in direct_deps(load_spec(name), "aarch64"):
                if dep == name or dep not in order:
                    continue
                with self.subTest(package=name, dependency=dep):
                    self.assertLess(order.index(dep), order.index(name),
                                    f"{dep} 应该排在 {name} 前面")


class TestEveryRealSpec(unittest.TestCase):
    """对仓库里每个真实配方做一遍结构性检查。

    这些不是形式主义：built_payload() 直接取 pkg['release']，缺了会
    KeyError；source.url 忘了写 $VERSION 会永远下载同一个版本。
    """

    def specs(self):
        for d in sorted(PACKAGES_DIR.iterdir()):
            if (d / "spec.toml").is_file():
                yield d.name

    def test_package_name_matches_directory(self):
        for name in self.specs():
            with self.subTest(package=name):
                self.assertEqual(load_spec(name)["package"]["name"], name)

    def test_release_present_and_int(self):
        for name in self.specs():
            with self.subTest(package=name):
                release = load_spec(name)["package"].get("release")
                self.assertIsInstance(release, int,
                                      f"{name} 的 [package] 缺少 release（或不是整数）")

    def test_sources_expand_cleanly(self):
        for name in self.specs():
            spec = load_spec(name)
            for src in spec.get("source", []):
                with self.subTest(package=name, url=src.get("url")):
                    url = expand(spec, src["url"])
                    self.assertNotIn("$", url, "URL 里有没展开的占位符")
                    self.assertTrue(url.startswith(("http://", "https://")))
                    self.assertRegex(src["sha256"], r"^[0-9a-f]{64}$")


if __name__ == "__main__":
    unittest.main(verbosity=2)
