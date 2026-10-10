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

from skirtlib import build as build_mod
from skirtlib import collect as collect_mod
from skirtlib import deps as deps_mod
from skirtlib.build import run_in_container
from skirtlib.common import PACKAGES_DIR, build_jobs

#: 依赖闭包测试要用的基线。直接用真实的 host/provides.txt——不要在这里
#: 另写一份：【另写一份就会漂移】，而它漂移的表现是测试莫名其妙地报环，
#: 或者更糟：报一个已经不存在的环。工具链和 autotools 都在里面，原因见
#: 那个文件自己的注释（它们都是「构建构建系统」的工具，靠排序解不开）。
from skirtlib.deps import load_host_provides as _load_host_provides
TOOLCHAIN_BASELINE = _load_host_provides()
from skirtlib.deps import load_host_provides, merge_tree, source_date_epoch
from skirtlib.spec import (deps_for, direct_deps, expand, is_subpackage,
                           latest_date, load_spec, package_date, recipe_of,
                           subpackages)


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


class TestBuildScriptDefaults(unittest.TestCase):
    """三个构建系统的默认参数。

    【为什么默认值本身要测】库目录曾经是个坑：autoconf 在 64 位系统上会把
    libdir 定成 ${prefix}/lib64，于是包装出一个真目录 /usr/lib64，和我们按
    0001 做的 usr-merge 符号链接撞车——拼 rootfs 时 cp 直接失败：

        cp: cannot overwrite non-directory '/rootfs/./usr/lib64'
            with directory '.../libffi-3.8.0-1/payload/./usr/lib64'

    实测中招的是 libffi。但这是一类问题，所以在默认值里钉死。这一条测试
    就是钉住那个钉子——不然哪天默认值被改回去，要等拼装时才炸。
    """

    def script(self, build):
        return build_mod.make_build_script({"package": {"name": "x"}, "build": build})

    def test_autotools_pins_libdir(self):
        s = self.script({"type": "autotools"})
        self.assertIn("--libdir=/usr/lib", s)
        self.assertIn("--prefix=/usr", s)

    def test_meson_pins_libdir(self):
        s = self.script({"type": "meson"})
        self.assertIn("--libdir=lib", s)

    def test_cmake_pins_libdir(self):
        s = self.script({"type": "cmake"})
        self.assertIn("-DCMAKE_INSTALL_LIBDIR=lib", s)

    def test_配方可以覆盖默认值(self):
        s = self.script({"type": "autotools",
                         "configure": ["--prefix=/opt"]})
        self.assertIn("--prefix=/opt", s)
        # 覆盖时用配方给的整套，不再附加默认值
        self.assertNotIn("--libdir=/usr/lib", s)


    def test_autoreconf_必须声明_autotools(self):
        """用 autoreconf 的配方必须把 autoconf/automake/libtool 写进 build。

        【为什么这条值得单独测】libxcrypt 就是靠它才暴露问题的：项目的源
        策略改成用 commit 之后，autotools 项目要现场跑 autoreconf，而它
        依赖的 autoconf/automake/libtool 如果不声明，就会悄悄用构建环境
        镜像里的那一份——和当初 binutils 那个漏声明是同一种病。

        规则很简单：用了什么工具，就声明什么。
        """
        import tomllib
        need = {"autoconf", "automake", "libtool"}
        checked = 0
        for d in sorted(PACKAGES_DIR.iterdir()):
            f = d / "spec.toml"
            if not f.is_file():
                continue
            spec = tomllib.loads(f.read_text())
            if not spec.get("build", {}).get("autoreconf"):
                continue
            checked += 1
            for arch in ("aarch64", "x86_64"):
                declared = set(direct_deps(spec, arch, kinds=("build",)))
                self.assertFalse(
                    need - declared,
                    f"{d.name} [{arch}]: 用了 autoreconf 但 build 里没有 "
                    f"{sorted(need - declared)}")
        # 不能因为一个都没有就静默通过——那正是「空验证」的形状
        self.assertGreater(checked, 0, "没有任何配方在用 autoreconf，这条测试是空的")

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
        # 原来这里断言 binutils 在闭包里。现在它在 host/provides.txt 里——
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
