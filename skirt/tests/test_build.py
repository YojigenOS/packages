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

class TestContainerCommand(unittest.TestCase):
    """构建容器收到的环境变量与加固标志。

    这一组参数决定产物是否可复现、是否有加固——而它们**丢掉时不会有任何
    报错**，只会让产物悄悄变得不一样。所以值得逐条钉住。
    """

    EPOCH = 1791646200

    def _cmd(self, arch: str = "aarch64") -> list[str]:
        spec = {"package": {"name": "x", "version": "1", "release": 1},
                "_dir": RECIPES_DIR}
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

    def test_构建脚本必须自带_set_e(self):
        """run.sh 必须自己 set -e，不能指望调用方。

        【为什么单独守着这一条】run.sh 是通过
        `setpriv ... bash "$WORK/run.sh"` 起的【另一个 bash 进程】，
        而 set -e 不会被 bash 子进程继承——写在外层 wrapper 上没用。

        没有它，脚本末尾那两行

            build
            package

        就变成 sequential 而不是 short-circuit：build 失败、package 照跑，
        run.sh 的退出码取自 package，而 package 往往能"成功"（哪怕只装进去
        一个文件）。外层看到 0，驱动打印「完成」。

        实测踩过：内核 make 报 Error 2、两条 install 都 cannot stat，驱动
        却说「[完成] linux-7.2.9-1（2 项）」——产物里只有一个 321 KB 的
        config 文件，而它还会被当成依赖合并进后面的构建。
        """
        for kind in ("autotools", "meson", "cmake", "custom"):
            with self.subTest(kind=kind):
                s = self.script({"type": kind})
                # 必须是脚本自己的第一件事，不能只在某个函数体内
                self.assertTrue(s.startswith("set -e"),
                                f"{kind}: 脚本开头没有 set -e")


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
        for d in _all_spec_dirs():
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


class TestCollectPreservesModes(unittest.TestCase):
    """收集产物时，**目录的模式也要带过来**。

    这一条是真出过事的：_write_package 用 dst.mkdir() 建目录，默认 0755，
    于是 filesystem 里 chmod 1777 的 /tmp 和 chmod 0700 的 /root 全变成 0755。
    文件那一路有 shutil.copy2 兜着，所以只有目录会中招——这也是它一直没被
    发现的原因：home/etc/var 本来就是 0755，看起来都对。

    代价实测过：本地自举跑到最后一个包 glibc，configure 报
    "config.guess: cannot create a temporary directory in /tmp"——因为构建是
    降权跑的，而 /tmp 成了 0755。

    merge_tree（deps.py）里早就写着同一件事，它那里修了，collect 这里漏了。
    """

    def test_目录模式要被保留(self):
        import stat
        from skirtlib import collect as collect_mod
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            dest = root / "dest"
            out = root / "out"
            # 一个 1777 的目录、一个 0700 的目录、一个普通文件
            (dest / "tmp").mkdir(parents=True)
            (dest / "root").mkdir(parents=True)
            (dest / "usr/bin").mkdir(parents=True)
            (dest / "usr/bin/hello").write_text("x")
            os.chmod(dest / "tmp", 0o1777)
            os.chmod(dest / "root", 0o700)

            wanted = {"tmp", "root", "usr", "usr/bin", "usr/bin/hello"}
            collect_mod._write_package(out, dest, wanted)

            payload = out / "payload"
            got_tmp = stat.S_IMODE(os.stat(payload / "tmp").st_mode)
            got_root = stat.S_IMODE(os.stat(payload / "root").st_mode)
            self.assertEqual(oct(got_tmp), oct(0o1777),
                             "/tmp 的 1777 丢了——构建会以莫名其妙的 Permission denied 失败")
            self.assertEqual(oct(got_root), oct(0o700), "/root 的 0700 丢了")
