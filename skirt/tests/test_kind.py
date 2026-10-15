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

class TestKindMatchesArtifacts(unittest.TestCase):
    """分类是【声明】的，但产物能检查它。

    【为什么必须是声明的】42 个包里有 17 个既装库又装命令（xz、util-linux、
    perl、python3、glibc…），产物根本分不出来。所以 kind 是作者写的事实，
    就像依赖一样。

    【为什么要检查】声明和事实会漂移——这一路已经栽过八次，都是"配了但没写"
    或"写了但不对"的隐式依赖。分类也一样：写错了不会报错，只会让"哪些是纯库"
    这类问题从此答错。

    检查是【必要条件】，不是充分条件：lang 必须装了解释器，但不排斥它也有
    .so。反过来不成立——有 .so 不等于它是 lib。
    """

    #: 共享库的文件名：「.so」结束，或者后面只跟版本数字（.so.1、.so.1.2.3）。
    #:
    #: 【为什么要写正则而不是 *.so*】第一版用 `*.so*`，于是 /etc/ld.so.conf
    #: 被当成了共享库，filesystem 就被报成「data 但有代码」。这类假阳性会让
    #: 人开始不信这条测试——那比没有测试更糟。
    _SO = re.compile(r"\.so(\.\d+)*$")

    @classmethod
    def setUpClass(cls):
        from skirtlib.common import OUTPUT_DIR
        cls.payloads = {}
        for name in spec_mod.all_recipe_names():
            # 【为什么要多滤一道】产物目录叫 <包名>-<版本>-<发行号>，而
            # glob 的 `*` 会跨越 `-`：包名 `linux` 的 f"*/linux-*/payload"
            # 会把 `linux-headers-7.2.9-1/payload` 也匹配上。
            #
            # 于是 linux 会被拿 linux-headers 的产物去检查，报一条「kernel 但
            # boot/ 下没有 vmlinuz」——错的是检查对象，不是包。
            #
            # 判据：包名后面紧跟的那个字符必须是数字（版本号总是数字开头）。
            # 只要有一个包名是另一个的前缀就会踩这个坑，而 linux/linux-headers
            # 正好是这一对。
            hits = [p for p in OUTPUT_DIR.glob(f"*/{name}-*/payload")
                    if p.parent.name[len(name)] == "-"
                    and p.parent.name[len(name) + 1:len(name) + 2].isdigit()]
            if hits:
                cls.payloads[name] = hits[0]

    def _shape(self, payload):
        so = any(self._SO.search(f.name) for f in payload.rglob("*")
                 if f.is_file() or f.is_symlink())
        so = so or bool(list(payload.rglob("*.a")))
        exe = False
        for sub in ("usr/bin", "usr/sbin", "bin", "sbin"):
            d = payload / sub
            if d.is_dir():
                for x in d.iterdir():
                    if x.is_file() and os.access(x, os.X_OK) and not x.is_symlink():
                        exe = True
                        break
        hdr = bool(list(payload.rglob("*.h")))
        return so, exe, hdr

    def test_kind_和产物一致(self):
        if not self.payloads:
            self.skipTest("var/output/ 里还没有产物——这条要看到产物才有意义")
        bad, checked = [], 0
        for name, payload in sorted(self.payloads.items()):
            kind = spec_mod.kind_of(name)
            so, exe, hdr = self._shape(payload)
            checked += 1
            if kind == "lang" and not exe:
                bad.append(f"{name}: lang 但 bin/ 里没有可执行文件")
            elif kind == "tool" and not exe:
                bad.append(f"{name}: tool 但 bin/ 里没有可执行文件")
            elif kind == "net" and not exe:
                # ★ 这一条是 2026-10-15 补的。`net` 那 7 个包
                #   （chrony curl dbus dhcpcd iproute2 iputils openssh）
                #   **一直在所有分支之外** —— 下面那串 elif 没有 `net`，
                #   于是它们掉进"什么都不查"里。
                #
                #   而它们该有的形态是明确的：都是【能跑的东西】（守护进程或
                #   客户端命令）。所以和 tool 同一条判据。
                bad.append(f"{name}: net 但 bin/ 里没有可执行文件")
            elif kind == "lib" and not so:
                bad.append(f"{name}: lib 但没有 .so/.a")
            elif kind == "data" and so:
                bad.append(f"{name}: data 但有 .so——数据包不该有代码")
            elif kind == "devel" and not (exe or hdr):
                # devel 给你的是「拿它来构建」的东西——工具【或者】头文件。
                # linux-headers 就是纯头文件包：一个可执行文件都没有，但没它
                # 你构建不了任何东西。
                bad.append(f"{name}: devel 但既没有可执行文件，也没有头文件")
            elif kind == "kernel" and not (payload / "boot" / "vmlinuz").exists():
                # 内核单独一类，因为它的产物形态是独有的：既不是库，也不是
                # usr/bin 下的命令，也没有头文件（那是 linux-headers 的事）。
                # 它是 /boot 底下一个能被引导器直接载入的镜像。
                #
                # 这条比 data 那条有意义：data 只要求「没有 .so」，一个空目录
                # 也能过；这条要求产物里真的有能启动的东西。
                bad.append(f"{name}: kernel 但 boot/ 下没有 vmlinuz")
            elif kind == "?":
                bad.append(f"{name}: 不在任何分类目录里")
        self.assertGreater(checked, 20, f"只查了 {checked} 个，太少了")
        self.assertFalse(bad, "分类和产物对不上：\n         " + "\n         ".join(bad))
