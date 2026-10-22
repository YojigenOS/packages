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
import pathlib
import tomllib
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
            run_in_container(".", "src.tar", arch, spec, self.EPOCH)
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

    def test_默认值在前_配方追加在后(self):
        """★ 2026-10-17 改的行为。

        原来是「配方写了就用配方的，默认值整个丢掉」，而那条注释把它写成了
        规格：「覆盖时用配方给的整套，不再附加默认值」。

        ★ 而那个规格会惩罚一种【自然】的写法 —— 只想写自己那几个选项的人，
          会静静地丢掉 `--prefix` 和 `--libdir`。实测的后果：iputils 装出了
          /usr/local/bin/ping（meson 自己的默认 prefix 是 /usr/local），
          而 usr/bin 里是空的；同一个原因还影响 dbus / libevent / fastfetch。

        现在默认值【永远在前面】，spec 的【追加】在后面。而 spec 里再写一次
        `--prefix` 也能覆盖（后面的赢）——那条路留着，只是不再是唯一的写法。
        """
        s = self.script({"type": "autotools",
                         "configure": ["--prefix=/opt"]})
        # 配方那个还在
        self.assertIn("--prefix=/opt", s)
        # ★ 而默认值【没有被丢掉】—— 这是这次改的核心
        self.assertIn("--libdir=/usr/lib", s)
        # ★ 而顺序是"默认在前、配方在后"，所以配方写的仍然是赢的那个
        self.assertLess(s.index("--libdir=/usr/lib"), s.index("--prefix=/opt"))

    def test_没写_configure_时用默认值(self):
        s = self.script({"type": "autotools"})
        self.assertIn("--prefix=/usr", s)
        self.assertIn("--libdir=/usr/lib", s)

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

class TestMetadataOnlyPackages(unittest.TestCase):
    """没有 [[source]] 的包也要能走完整条路。

    ★ 这是 CI 上踩的：`unpack` 只在 `if spec.get("source")` 分支里赋值，于是
      filesystem / pip 这类纯元数据包走 else，到
      run_in_container(..., unpack=unpack) 就报

          UnboundLocalError: cannot access local variable 'unpack'

      本地没发现，因为建 pip 的时候 unpack 那个功能还没加，加完就没再建过它。
    """

    def test_unpack_defined_outside_the_source_branch(self):
        src = (pathlib.Path(__file__).resolve().parents[1]
               / "skirtlib" / "commands.py").read_text()
        # 默认值必须在 `if spec.get("source")` 之前出现
        i_default = src.index("unpack = True")
        i_branch = src.index('if spec.get("source"):')
        self.assertLess(i_default, i_branch,
                        "unpack 的默认值要在 source 分支【之前】——否则纯元数据包会炸")

class TestAutotoolsPassesBuildAlongsideHost(unittest.TestCase):
    """给 autotools 包加 --host 时，必须【连 --build 一起传】，而且同一个三元组。

    ★ 这是 CI 上踩的：只传 --host=aarch64-yojigen-linux-gnu 时，configure 拿
      config.guess 得到的 build 和它一比发现不一样，就认为在【交叉编译】——
      于是去找带前缀的工具：

          aarch64-yojigen-linux-gnu-ar: command not found
          configure: error: could not determine aarch64-yojigen-linux-gnu-ar interface

      binutils 第一个炸，gcc 也会。实测（binutils 2.47，Debian 环境）：

          --host=X 单独          →  AR = X-ar     ✗ 那个工具不存在
          --build=X --host=X     →  AR = ar       ✓

      build == host 时 configure 走【本地构建】那条路，用不带前缀的工具名，
      同时把产物记录成我们的三元组——这正是我们要的：换个名字的本地构建。

    ★ 这个错在 devel 镜像里【看不出来】：那里没有 binutils 源码，而且 gcc 是
      我们自己建的、已经带了别的三元组。只有从 Debian 引导那一轮才会撞上。
    """

    def test_host_is_always_accompanied_by_build(self):
        src = (pathlib.Path(__file__).resolve().parents[1]
               / "skirtlib" / "build.py").read_text()
        self.assertIn('opts_list.append(f"--build={triple}")', src,
                      "--host 必须配一个同值的 --build")
        self.assertIn('opts_list.append(f"--host={triple}")', src)
        # --build 要排在 --host 前面（读起来是"给谁建、建在哪"的顺序）
        self.assertLess(src.index('--build={triple}'), src.index('--host={triple}'))

    def test_gcc_steps_also_passes_build(self):
        """gcc 是 custom——驱动的注入到不了它，得在 steps.sh 里自己写。"""
        sh = (pathlib.Path(__file__).resolve().parents[2]
              / "recipes" / "devel" / "gcc" / "steps.sh").read_text()
        self.assertIn('--build="$PKGTRIPLE"', sh,
                      "gcc 的 configure 也要有 --build，否则它也会去找带前缀的 ar")
        self.assertIn('--host="$PKGTRIPLE"', sh)
        self.assertIn('--target="$PKGTRIPLE"', sh)

class Test配方里的键都是真的(unittest.TestCase):
    """★ [build] 里写错的键【不会报错】，只会被静默忽略。

    这是实测撞到的：给 fastfetch 写了 13 个 CMake 选项，键名写成 `options`
    （正确的是 `configure`）。包建成功了——用默认值编的，少了 -DENABLE_ZLIB、
    -DENABLE_WCWIDTH 那些。界面是绿的，产物是错的。

    所以这里对【所有配方】过一遍：每个键都得是驱动真的会读的。
    驱动那边也加了一道运行时检查（make_build_script 里），但那个只在构建时
    触发；这个测试让它在你提交之前就炸。
    """

    # 从驱动代码里数出来的——grep 一遍所有 b.get(...)
    # ★ 这份清单是【第二份】—— 而"两份清单要一致"这件事正是
    #   `test_vendor.py` 里 `test_白名单和_build_读的键一致` 守着的那个形状。
    #   加 `vendor` / `vendor_check` 时两处都要改（2026-10-22，接 docker）。
    KNOWN = {"type", "out_of_tree", "autoreconf", "patches",
             "configure", "configure_shell", "nostrip", "cflags",
             "vendor", "vendor_check"}
    KINDS = {"autotools", "meson", "cmake", "custom"}

    def test_所有配方的_build_键都是真的(self):
        recipes = sorted(pathlib.Path(__file__).resolve().parents[2].glob("recipes/*/*/spec.toml"))
        self.assertGreater(len(recipes), 40, "配方数量不对，路径大概错了")
        bad = []
        for sp in recipes:
            spec = tomllib.loads(sp.read_text())
            b = spec.get("build", {})
            kind = b.get("type", "custom")
            name = sp.parent.relative_to(sp.parents[2])
            if kind not in self.KINDS:
                bad.append(f"{name}: 构建类型 {kind!r} 不认识"
                           f"（只有 {sorted(self.KINDS)}）")
                continue
            extra = sorted(set(b) - self.KNOWN)
            if extra:
                bad.append(f"{name}: [build] 里多余的键 {extra}")
        self.assertFalse(bad, "这些键写错了，而写错【不会报错】只会被静默忽略：\n  " + "\n  ".join(bad))


class Test不许有改了不生效的配置(unittest.TestCase):
    """★ `steps.sh` 重定义了 `build()` 时，spec 里的 `[build] configure` 是【死代码】。

    机理（`build.py` 生成的 `run.sh`）：

        build() { <spec 里那份 configure> }        ← 先定义
        if [ -f "$WORK/steps.sh" ]; then . "$WORK/steps.sh"; fi    ← 再 source
        "# steps.sh 若存在，可以通过重定义上面任意函数来覆盖默认行为"

    所以 `steps.sh` 里的 `build()` 整个把它盖掉。**危险的不是重复，是
    「改在这一份上不会生效而没有任何提示」** —— 而 `procps/steps.sh` 里
    自己就写着一句"写两处会不一致"。

    2026-10-15 有四个配方是这个形状（chrony / procps / strace / lsof），
    spec 里那份全删了（删之前逐条比对过，两边内容一致）。
    """

    def test_重定义了_build_就不该在_spec_里留_configure(self):
        import pathlib as _p
        import re
        import tomllib
        from skirtlib.common import RECIPES_DIR

        坏 = []
        for spec in sorted(_p.Path(RECIPES_DIR).rglob("spec.toml")):
            steps = spec.parent / "steps.sh"
            if not steps.is_file():
                continue
            正文 = re.sub(r"^\s*#.*$", "", steps.read_text(), flags=re.M)
            if not re.search(r"^\s*build\s*\(\)\s*\{", 正文, re.M):
                continue
            d = tomllib.loads(spec.read_text())
            if "configure" in (d.get("build") or {}):
                坏.append(spec.parent.name)
        self.assertFalse(坏, "这些配方的 steps.sh 重定义了 build()，"
                             "而 spec 里还留着一份永远不会生效的 configure：\n  "
                             + "\n  ".join(坏))
