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
from skirtlib.common import PACKAGES_DIR, build_jobs

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

class TestLoadSpec(unittest.TestCase):
    def test_missing_package_exits(self):
        with self.assertRaises(SystemExit):
            load_spec("这个包根本不存在")

    def test_real_package(self):
        s = load_spec("zlib")
        self.assertEqual(s["package"]["name"], "zlib")
        self.assertIn("_dir", s, "load_spec 应该把配方目录放进 _dir")

class TestRecipeOf(unittest.TestCase):
    """子包名 → 配方名的解析。"""

    def test_recipe_resolves_to_itself(self):
        self.assertEqual(recipe_of("glibc"), "glibc")

    def test_subpackage_resolves_to_parent(self):
        self.assertEqual(recipe_of("glibc-dev"), "glibc")
        self.assertEqual(recipe_of("glibc-locales"), "glibc")

    def test_unknown_is_none(self):
        self.assertIsNone(recipe_of("没有这个包"))

    def test_is_subpackage(self):
        self.assertTrue(is_subpackage("glibc-dev"))
        self.assertFalse(is_subpackage("glibc"))
        self.assertFalse(is_subpackage("没有这个包"))

class TestSubpackagePatterns(unittest.TestCase):
    """子包的文件分流规则。

    分流错了的后果很隐蔽：文件不会消失，只会跑到另一个包里，然后产出一个
    「装得上、跑不起来」的系统。所以匹配规则本身值得逐个钉住。
    """

    def test_directory_prefix(self):
        """`usr/include/` 命中它自己和底下的一切，但不命中同前缀的兄弟。"""
        self.assertTrue(collect_mod._pattern_match("usr/include", "usr/include/"))
        self.assertTrue(collect_mod._pattern_match("usr/include/stdio.h", "usr/include/"))
        self.assertTrue(collect_mod._pattern_match("usr/include/sys/types.h", "usr/include/"))
        self.assertFalse(collect_mod._pattern_match("usr/includes", "usr/include/"))
        self.assertFalse(collect_mod._pattern_match("usr/lib/libc.a", "usr/include/"))

    def test_glob_does_not_cross_slash(self):
        self.assertTrue(collect_mod._pattern_match("usr/lib/libc.a", "usr/lib/*.a"))
        self.assertFalse(collect_mod._pattern_match("usr/lib/sub/libc.a", "usr/lib/*.a"))
        self.assertFalse(collect_mod._pattern_match("usr/lib/libc.so.6", "usr/lib/*.a"))

    def test_exact_path(self):
        self.assertTrue(collect_mod._pattern_match("usr/lib/libc.so", "usr/lib/libc.so"))
        self.assertFalse(collect_mod._pattern_match("usr/lib/libm.so", "usr/lib/libc.so"))

    def test_first_match_wins(self):
        """先声明的子包获胜——顺序是配方作者控制分流的手段。"""
        subs = [
            {"name": "a", "files": ["usr/lib/*.a"]},
            {"name": "b", "files": ["usr/lib/"]},
        ]
        self.assertEqual(collect_mod._owner("usr/lib/libc.a", subs), "a")
        self.assertEqual(collect_mod._owner("usr/lib/libc.so.6", subs), "b")

    def test_no_match_means_main_package(self):
        subs = [{"name": "a", "files": ["usr/include/"]}]
        self.assertIsNone(collect_mod._owner("usr/bin/ls", subs))

    def test_glibc_rules_put_runtime_files_in_the_main_package(self):
        """对着 glibc 的真实规则跑一遍。

        这几条是反例：模式匹配没法区分「符号链接」和「真的 ELF」，
        而 /usr/lib/libmemusage.so 是真库、libnss_db.so 是链接期符号链接。
        所以规则只搬真正占体积的四类，其余一律留在主包。
        """
        subs = subpackages(load_spec("glibc"))
        for rel in ("usr/lib/libc.so.6", "usr/lib/ld-linux-aarch64.so.1",
                    "usr/lib/libmemusage.so", "usr/lib/libnss_db.so",
                    "usr/bin/ldd"):
            with self.subTest(path=rel):
                self.assertIsNone(collect_mod._owner(rel, subs))
        for rel, owner in (("usr/include/stdio.h", "glibc-dev"),
                           ("usr/lib/libc.a", "glibc-dev"),
                           ("usr/share/i18n/charmaps/UTF-8.gz", "glibc-dev"),
                           ("usr/lib/locale/zh_CN.utf8/LC_CTYPE", "glibc-locales")):
            with self.subTest(path=rel):
                self.assertEqual(collect_mod._owner(rel, subs), owner)

class TestSubpackageParsing(unittest.TestCase):
    """[[subpackage]] 的字段校验。"""

    def _spec(self, **sub):
        d = {"name": "x-dev", "files": ["usr/include/"]}
        d.update(sub)
        return {"package": {"name": "x"}, "_dir": Path("/tmp/x"), "subpackage": [d]}

    def test_ok(self):
        subs = subpackages(self._spec())
        self.assertEqual(subs[0]["name"], "x-dev")

    def test_missing_name_dies(self):
        spec = {"package": {"name": "x"}, "_dir": Path("/tmp/x"),
                "subpackage": [{"files": ["a/"]}]}
        with self.assertRaises(SystemExit):
            subpackages(spec)

    def test_missing_files_dies(self):
        with self.assertRaises(SystemExit):
            subpackages(self._spec(files=[]))

    def test_same_name_as_main_dies(self):
        """子包和主包同名会让两者互相覆盖，而且是静默的。"""
        with self.assertRaises(SystemExit):
            subpackages(self._spec(name="x"))
