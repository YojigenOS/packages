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

    #: payload → "看了什么"的描述。报错时打出来，好让"为什么没找到"有线索。
    _看了: dict[int, str] = {}

    def _shape(self, payload):
        """(有没有 .so, 有没有可执行文件, 有没有头文件)。

        ★★ 2026-10-17：顺手记下"看了哪儿、看到什么"。

        原来只返回三个布尔值，于是失败时那句话是：

            meson: devel 但既没有可执行文件，也没有头文件

        —— 而它不说【它找了哪几个目录】、【那些目录在不在】、【里面有什么】。
        而那三样正好是"为什么没找到"的全部信息。本地复现时 meson 的 payload
        明明有 `usr/bin/meson` 且 `exe=True`，CI 里却报没有 —— 中间那一段
        完全是黑的。

        所以现在把过程存进 self._看了，报错时一并打出来。
        「一个该说话的地方没说话」——这一处就是。
        """
        so = any(self._SO.search(f.name) for f in payload.rglob("*")
                 if f.is_file() or f.is_symlink())
        so = so or bool(list(payload.rglob("*.a")))
        exe = False
        看了: list[str] = []
        for sub in ("usr/bin", "usr/sbin", "bin", "sbin"):
            d = payload / sub
            if not d.is_dir():
                看了.append(f"{sub}(不在)")
                continue
            项 = sorted(d.iterdir())
            if not 项:
                看了.append(f"{sub}(空)")
                continue
            详情 = []
            for x in 项[:4]:
                # ★★ 2026-10-17：去掉了 `not x.is_symlink()`。
                #
                #   它【完全多余】，而它唯一的效果是【把好的链接也判死】：
                #
                #       好链接（vim → vim.basic）  旧判据 False ✗   现在 True ✓
                #       断链                       旧判据 False      现在 False ✓
                #       普通文件                   旧判据 True       现在 True ✓
                #
                #   因为 `os.access(x, os.X_OK)` 【自己就会跟链接过去】——断链
                #   自然返回 False。那个条件防的东西，`os.access` 本来就防住了。
                #
                #   ★ 实测的后果：vim 和 xz 被判成"没有可执行文件"，而它们的
                #     `usr/bin` 里明明有 9 个和 23 个（只是大多是链接）。而包
                #     【真的能用】—— 真机上 vim 能跑。**一条为了防备某个情况
                #     而加的条件，在别处已经挡住那个情况之后，只剩副作用。**
                可 = os.access(x, os.X_OK)
                # 链接的话，把指向写出来 —— 不然只看到一排 [✗] 会以为是权限
                if x.is_symlink():
                    详情.append(f"{x.name}→{os.readlink(x)}[{'ok' if 可 else '✗'}]")
                else:
                    详情.append(f"{x.name}[{'ok' if 可 else '✗'}]")
                if 可:
                    exe = True
            看了.append(f"{sub}({len(项)}): " + " ".join(详情))
        # ── ★★ 2026-10-22：`usr/libexec` 也算 ──────────────────────────────
        #
        # 起因：接 docker 的两个插件（`docker-buildx` / `docker-compose`）时这条
        # 判据把它们判成了「tool 但 bin/ 里没有可执行文件」。
        #
        # 而它们的 kind 确实是对的：别人依赖它们是为了【在命令行跑】
        # （`docker buildx build …`），只是那个程序不叫 buildx、也不在 PATH 上
        # —— 它叫 `docker-buildx`，装在 CLI 找插件的目录里。
        #
        # ★ 而 `usr/libexec` 这个词的意思本来就是"系统私有的可执行文件"，
        #   把它排除在外是这条判据的一个漏洞，不是一种严格。
        #
        # ★★ 但它必须【递归】找、且只认普通文件：`os.access(目录, os.X_OK)`
        #   对目录也是 True —— 直接按 usr/bin 那套写，任何装了一个空目录的包
        #   都会算"有可执行文件"。那是把判据变松，不是修好它。
        libexec = payload / "usr/libexec"
        if libexec.is_dir():
            # `is_file()` 会跟着链接走 —— 和上面那条去掉 `not is_symlink()`
            # 的注释是同一个道理。
            可执行 = [x for x in libexec.rglob("*")
                      if x.is_file() and os.access(x, os.X_OK)]
            if 可执行:
                exe = True
            看了.append(f"usr/libexec({len(可执行)} 个可执行): "
                        + " ".join(x.name for x in 可执行[:4]))
        else:
            看了.append("usr/libexec(不在)")

        hdr = bool(list(payload.rglob("*.h")))

        # ★★ 纯 Python 的包：装到 site-packages 下，是 .py + dist-info
        #   （2026-10-22 加。撞到的是 flit-core / jinja2 / markupsafe /
        #   setuptools —— 四个都是"拿它来构建"的东西，而它们既没有可执行
        #   文件也没有 C 头文件）。
        #
        #   ★ 判据用 site-packages 下的 *.py，不用"payload 里有 .py" ——
        #     后者太宽（任何一个包顺手带个脚本都算）。
        py = bool(list(payload.rglob("usr/lib/python3*/site-packages/**/*.py")))
        if py:
            hdr = True
        文件 = [str(f.relative_to(payload)) for f in payload.rglob("*") if f.is_file()]
        # ★★ 文件少的时候【把它们列出来】—— 2026-10-17。
        #   那两轮里 meson 和 pip 都是"payload 里 1 个文件"，而"1 个"不说
        #   那 1 个是什么。而它是什么，正是"为什么解出来是这样"的全部线索。
        名单 = ("：" + "、".join(文件[:5]) + ("…" if len(文件) > 5 else "")
                ) if len(文件) <= 20 else ""
        self._看了[id(payload)] = (f"payload 里 {len(文件)} 个文件{名单}；"
                                   + "；".join(看了))
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
                bad.append(f"{name}: lang 但没有可执行文件（找过 usr/bin usr/sbin bin sbin usr/libexec）")
            elif kind == "tool" and not exe:
                bad.append(f"{name}: tool 但没有可执行文件（找过 usr/bin usr/sbin bin sbin usr/libexec）")
            elif kind == "net" and not exe:
                # ★ 这一条是 2026-10-15 补的。`net` 那 7 个包
                #   （chrony curl dbus dhcpcd iproute2 iputils openssh）
                #   **一直在所有分支之外** —— 下面那串 elif 没有 `net`，
                #   于是它们掉进"什么都不查"里。
                #
                #   而它们该有的形态是明确的：都是【能跑的东西】（守护进程或
                #   客户端命令）。所以和 tool 同一条判据。
                bad.append(f"{name}: net 但没有可执行文件（找过 usr/bin usr/sbin bin sbin usr/libexec）")
            elif kind == "lib" and not so:
                bad.append(f"{name}: lib 但没有 .so/.a")
            elif kind == "data" and so:
                bad.append(f"{name}: data 但有 .so——数据包不该有代码")
            elif kind == "devel" and not (exe or hdr):
                # devel 给你的是「拿它来构建」的东西——工具【或者】头文件
                # 【或者 Python 模块】。
                # linux-headers 是纯头文件包；jinja2 是纯 Python 包（只有
                # site-packages 里的 .py）。两个都是"一个可执行文件都没有，但
                # 没它你构建不了任何东西"。
                bad.append(f"{name}: devel 但既没有可执行文件，也没有头文件，"
                           f"也没有 Python 模块")
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
        if bad:
            # ★ 把"看了什么"一起打出来 —— 否则这句话只说"没找到"，
            #   而不说找了哪儿、那儿有什么。
            细节 = "\n         ".join(
                f"{n} → {self._看了.get(id(self.payloads[n]), '（没记录）')}"
                for n in sorted(self.payloads) if any(b.startswith(f"{n}:") for b in bad))
            self.assertFalse(bad, "分类和产物对不上：\n         "
                             + "\n         ".join(bad) + "\n      看过的：\n         " + 细节)


class Test_libexec_也算可执行位置(unittest.TestCase):
    """docker 的 CLI 插件装在 `usr/libexec/docker/cli-plugins/`。

    ★ 2026-10-22：接 `docker-buildx` / `docker-compose` 时，`test_kind` 把它们
      判成「tool 但 bin/ 里没有可执行文件」。而它们的 kind 是对的 —— 别人依赖
      它们是为了在命令行跑（`docker buildx build …`），只是那个程序不在 PATH 上。

    ★★ 而"加一条路径"这件事必须小心：`os.access(目录, os.X_OK)` 对目录也是
      True，所以如果不限定"普通文件"，任何装了一个**空目录**的包都会算"有
      可执行文件"—— 那是把判据变松，而不是修好它。所以下面第二条专门盯它。
    """

    def _shape(self, 文件们):
        from tempfile import TemporaryDirectory
        from pathlib import Path
        from test_kind import TestKindMatchesArtifacts
        类 = TestKindMatchesArtifacts("test_kind_和产物一致")
        类._看了 = {}
        with TemporaryDirectory() as d:
            root = Path(d)
            for rel, mode in 文件们:
                f = root / rel
                f.parent.mkdir(parents=True, exist_ok=True)
                if mode is None:
                    f.mkdir(parents=True, exist_ok=True)
                else:
                    f.write_text("#!/bin/sh\n", encoding="utf-8")
                    f.chmod(mode)
            so, exe, hdr = 类._shape(root)
            return so, exe, hdr, 类._看了[id(root)]

    def test_插件装在_libexec_里就算有可执行文件(self):
        so, exe, hdr, 看了 = self._shape([
            ("usr/libexec/docker/cli-plugins/docker-buildx", 0o755),
        ])
        self.assertTrue(exe, f"该认出来，而它说：{看了}")

    def test_但空目录不算(self):
        """★★ 这一条守的是"别把判据变松"。"""
        so, exe, hdr, 看了 = self._shape([
            ("usr/libexec/docker/cli-plugins", None),
        ])
        self.assertFalse(exe, f"空目录不该算可执行文件，而它说：{看了}")
