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

class TestEveryRealSpec(unittest.TestCase):
    """对仓库里每个真实配方做一遍结构性检查。

    这些不是形式主义：built_payload() 直接取 pkg['release']，缺了会
    KeyError；source.url 忘了写 $VERSION 会永远下载同一个版本。
    """

    def specs(self):
        for d in _all_spec_dirs():
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

    def test_date_present_and_parseable(self):
        """每个配方都要有 date，且能算出 SOURCE_DATE_EPOCH。

        缺了它构建会直接失败——这是刻意的：宁可报错，也不要悄悄产出一个
        不可复现的包。
        """
        host_provides = load_host_provides()
        for name in self.specs():
            with self.subTest(package=name):
                spec = load_spec(name)
                self.assertIsInstance(
                    spec["package"].get("date"), datetime.datetime,
                    f"{name} 的 [package] 缺少 date（或不是 TOML 日期时间）")
                self.assertGreater(
                    source_date_epoch(spec, "aarch64", host_provides), 0)
                # 不该是「刚刚」或某个越界的默认值
                self.assertLess(
                    source_date_epoch(spec, "aarch64", host_provides), 2000000000)

    def test_dependency_can_push_it_later(self):
        """在真实配方上验证传播：glibc 的构建依赖里有 binutils。"""
        host_provides = load_host_provides()
        glibc = load_spec("glibc")
        deps = [load_spec(n)
                for n in deps_mod.dep_closure(glibc, "aarch64", host_provides)]
        # 原来这里断言 binutils 在闭包里。现在它在 bootstrap/provides.txt 里——
        # 工具链属于构建环境的起点（编译器没法依赖自己：gcc 要 gmp，gmp 要
        # 编译器）。基线里的包不参与闭包，所以换成断言 bison：它是 glibc 的
        # 构建依赖，而且【不在】基线里，同样能验证「依赖把包推到后面」。
        names = [s["package"]["name"] for s in deps]
        self.assertIn("bison", names)
        self.assertNotIn("binutils", names)   # 在基线里 → 不进闭包
        self.assertGreaterEqual(latest_date(glibc, deps), package_date(glibc))

    def test_sources_expand_cleanly(self):
        """源的地址要能干净展开——不能留没替换的变量。

        两种形式都测：url 形式（发行包）和 git 形式（仓库 + commit）。
        git 形式的地址是驱动算出来的，所以配方里【不该】出现托管商的链接；
        这条测试守的就是这个：配方写的是仓库，不是链接。
        """
        for name in self.specs():
            spec = load_spec(name)
            for src in spec.get("source", []):
                with self.subTest(package=name, src=src.get("url") or src.get("git")):
                    if src.get("git"):
                        git = expand(spec, src["git"])
                        self.assertNotIn("$", git,
                                         f"{name}: git 地址里有没展开的变量")
                        self.assertRegex(src.get("commit", ""), r"^[0-9a-f]{40}$",
                                         f"{name}: git 形式要写完整的 40 位 commit")
                        for mirror in src.get("mirrors", []):
                            self.assertNotIn(
                                "$", expand(spec, mirror),
                                f"{name}: 镜像地址里有没展开的变量")
                        continue

                    url = expand(spec, src["url"])
                    self.assertNotIn("$", url, f"{name}: URL 里有没展开的变量")
                    for extra in src.get("urls", []):
                        self.assertNotIn("$", expand(spec, extra),
                                         f"{name}: 备用 URL 里有没展开的变量")

    def test_subpackage_names_are_globally_unique(self):
        """子包名在整个仓库里必须唯一。

        它们没有自己的目录——只靠名字在 output/ 和依赖里被找到。重名会让
        recipe_of 返回先扫到的那个配方，于是产出一个装错东西的包。
        """
        seen: dict[str, str] = {}
        for name in self.specs():
            for sub in subpackages(load_spec(name)):
                with self.subTest(subpackage=sub["name"]):
                    self.assertNotIn(
                        sub["name"], seen,
                        f"{sub['name']} 同时被 {seen.get(sub['name'])} 和 {name} 声明")
                    seen[sub["name"]] = name

    def test_subpackage_names_do_not_collide_with_recipes(self):
        """子包不能和任何配方同名，否则「按名字找配方」会有两个答案。"""
        recipes = set(self.specs())
        for name in self.specs():
            for sub in subpackages(load_spec(name)):
                with self.subTest(subpackage=sub["name"]):
                    self.assertNotIn(sub["name"], recipes)

    def test_every_subpackage_resolves_back(self):
        for name in self.specs():
            for sub in subpackages(load_spec(name)):
                with self.subTest(subpackage=sub["name"]):
                    self.assertEqual(recipe_of(sub["name"]), name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
