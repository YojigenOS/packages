#!/usr/bin/env python3
"""`common.py` 的常量必须只定义一次。

【为什么值得一条测试】2026-10-11 发现这个文件里有一段被复制了三遍：

    第  17-34 行   OUTPUT_DIR  BUILD_DIR  HOST_PROVIDES_FILE ...
    第  51-68 行   同一批，一模一样
    第 106-123 行  同一批，一模一样

看形状是编辑时误粘：第 50 行和第 105 行都紧跟在【注释块】后面，像是有人
在改注释的时候把下面一整段常量又贴了一次。

**后果不是报错，是静默失效。** Python 里后定义覆盖前定义，所以实际生效的
是最后一份——改第一份不会报错，只是没用。而那次正要做的恰恰是「把产物
目录集中到 build/」，如果没发现这件事，改出来的结果是"改了但没生效"。

有一处更细的：`GNU_MIRROR` 被定义了两次，后一次把前一次覆盖了，于是
`GNU_MIRROR` 不再是 `GNU_MIRRORS[0]`。它当时已经没有任何调用方（备选源
改成了写在配方里），所以没造成故障——但它是同一种病的第二个症状。

这和项目里那条原则是同一件事：

    一处信息只该有一处。留着它就会有人用它，然后两处不一致。
"""

from __future__ import annotations

import ast
import collections
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "skirt"))

COMMON = ROOT / "skirt" / "skirtlib" / "common.py"


class TestCommonConstants(unittest.TestCase):
    def test_每个模块级常量只定义一次(self):
        tree = ast.parse(COMMON.read_text())
        defines = collections.Counter()
        for node in tree.body:
            # 只统计模块级的简单赋值：CONST = ...
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name) and t.id.isupper():
                        defines[t.id] += 1
            # 带类型标注的：CONST: str = ...
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                if node.target.id.isupper():
                    defines[node.target.id] += 1

        dup = {k: v for k, v in defines.items() if v > 1}
        self.assertFalse(
            dup,
            "common.py 里有常量被定义了多次（后定义的会静默覆盖前面的，"
            "改第一处不会生效）：\n         "
            + "\n         ".join(f"{k} 定义了 {v} 次" for k, v in sorted(dup.items())))

    def test_路径常量都指向仓库里真实存在的目录(self):
        """常量写错路径不会报错，只会在用到时才发现——所以在这里就查。"""
        from skirtlib import common

        # VARIANTS_DIR 本来也在这里，2026-10-11 风味搬到 distro 仓库去了。
        for name in ("RECIPES_DIR", "HOST_PROVIDES_FILE"):
            path = getattr(common, name)
            self.assertTrue(path.exists(), f"{name} 指向的 {path} 不存在")

        # 产物目录是构建时才创建的，所以只查它们【在仓库里】，不查具体哪一层。
        #
        # 这条断言我写错过两次，都被它自己抓出来了：
        #   第一版：把三个都写成"应该在仓库外面"——实际只有 CCACHE_DIR 在外面
        #   第二版：写成"parent 必须是 ROOT"——后来又都挪进了 var/
        # **写错的是我的假设，不是代码。** 所以现在只断言真正稳定的那件事：
        # 它们都在仓库里（这样 ls 一眼能分清源码和产物），而 CCACHE_DIR 在
        # 仓库外（它体积大、完全可再生，不该跟着仓库走）。
        for name in ("OUTPUT_DIR", "BUILD_DIR", "LOG_DIR"):
            path = getattr(common, name)
            self.assertTrue(path.is_relative_to(common.ROOT),
                            f"{name} 应该在仓库里，实际是 {path}")

        self.assertFalse(common.CCACHE_DIR.is_relative_to(common.ROOT),
                         "CCACHE_DIR 不该在仓库里（它体积大且完全可再生）")
