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

import datetime
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from skirtlib import deps as deps_mod
from skirtlib.build import run_in_container
from skirtlib.common import PACKAGES_DIR, build_jobs
from skirtlib.deps import load_host_provides, merge_tree, source_date_epoch
from skirtlib.spec import (deps_for, direct_deps, expand, latest_date, load_spec,
                           package_date)


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


def _dt(text: str) -> datetime.datetime:
    """把 "2026-10-10T15:30:00Z" 这种写法变成带时区的 datetime。"""
    return datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))


def _pkg(name: str, date, **extra) -> dict:
    return {"package": {"name": name, "version": "1", "release": 1,
                        "date": date, **extra}}


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


class TestContainerCommand(unittest.TestCase):
    """构建容器收到的环境变量与加固标志。

    这一组参数决定产物是否可复现、是否有加固——而它们**丢掉时不会有任何
    报错**，只会让产物悄悄变得不一样。所以值得逐条钉住。
    """

    EPOCH = 1791646200

    def _cmd(self, arch: str = "aarch64") -> list[str]:
        spec = {"package": {"name": "x", "version": "1", "release": 1},
                "_dir": PACKAGES_DIR}
        with mock.patch("skirtlib.build.subprocess.run") as run:
            run.return_value.returncode = 0
            run_in_container(".", arch, spec, self.EPOCH)
        return run.call_args[0][0]

    def _wrapper(self, arch: str = "aarch64") -> str:
        return self._cmd(arch)[-1]

    def test_reproducibility_env(self):
        joined = " ".join(self._cmd())
        for expected in (f"SOURCE_DATE_EPOCH={self.EPOCH}",
                         "LC_ALL=C.UTF-8",
                         "TZ=UTC"):
            with self.subTest(arg=expected):
                self.assertIn(expected, joined)

    def test_hostname_is_fixed(self):
        """docker 默认给容器随机主机名，调用 gethostname() 的构建会嵌入它。"""
        cmd = self._cmd()
        self.assertIn("--hostname", cmd)
        self.assertEqual(cmd[cmd.index("--hostname") + 1], "build")

    def test_umask_and_workdir(self):
        wrapper = self._wrapper()
        self.assertIn("umask 022", wrapper)
        self.assertIn("WORK=/build", wrapper)

    def test_hardening_flags(self):
        wrapper = self._wrapper()
        for flag in ("-g0",
                     "-fstack-protector-strong",
                     "-U_FORTIFY_SOURCE",
                     "-D_FORTIFY_SOURCE=3",
                     "-Wl,-z,relro",
                     "-Wl,-z,now"):
            with self.subTest(flag=flag):
                self.assertIn(flag, wrapper)

    def test_arch_specific_hardening(self):
        """加固标志是分架构的：arm64 用 BTI/PAC，x86_64 用 CET。"""
        self.assertIn("-mbranch-protection=standard", self._wrapper("aarch64"))
        self.assertIn("-fcf-protection=full", self._wrapper("x86_64"))


class TestBuildJobs(unittest.TestCase):
    """并行度：默认留两个核给用户，别把开发机占满。"""

    def test_leaves_two_cores_free(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch("skirtlib.common.os.cpu_count", return_value=10):
                self.assertEqual(build_jobs(), 8)

    def test_never_below_one(self):
        """只有 2 核的机器上，留两个核会算出 0——那会让 make 报错。"""
        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch("skirtlib.common.os.cpu_count", return_value=2):
                self.assertEqual(build_jobs(), 1)

    def test_env_override(self):
        with mock.patch.dict(os.environ, {"SKIRT_JOBS": "3"}):
            self.assertEqual(build_jobs(), 3)

    def test_bad_override_exits(self):
        for bad in ("abc", "0", "-1", "3.5"):
            with self.subTest(value=bad):
                with mock.patch.dict(os.environ, {"SKIRT_JOBS": bad}):
                    with self.assertRaises(SystemExit):
                        build_jobs()


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
        self.assertIn("binutils", [s["package"]["name"] for s in deps])
        self.assertGreaterEqual(latest_date(glibc, deps), package_date(glibc))

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
