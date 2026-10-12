#!/usr/bin/env python3
"""产物收集与子包拆分。

一个配方可以产出多个包：主包加上 `[[subpackage]]` 声明的那些。拆分发生在
collect.py，不在构建脚本里——构建照旧把一切装进 $PKGDEST，所以调整拆分规则
只要重跑收集。

这里测的是拆分的【元数据】部分：谁依赖谁。文件怎么分流在别处测。
"""

from __future__ import annotations

import pathlib
import unittest

LIB = pathlib.Path(__file__).resolve().parents[1] / "skirtlib"


class TestSubpackageDoesNotDependOnParent(unittest.TestCase):
    """子包【不】该依赖主包。

    ★ 这是拆 openssl 时发现的。原来的代码是：

        sub_depends = [pkg["name"]] + [d for d in runtime if d != pkg["name"]]
        # 子包依赖主包：它的文件是主包的一部分，单独装没有意义。

    **那个前提在构建期成立，在安装语义上是反的。** 子包的文件确实是从主包的
    $PKGDEST 分出来的，但分完之后主包的 payload 里【已经没有了】——那是分流
    的意思。带着那条依赖：

        装 libssl  → 拖进 openssl 主包 17 MB
        装 libgcc  → 拖进 gcc 主包 253 MB

    而那正好是拆子包要避免的事。

    ★ 对照 Alpine：它的 libgcc 只依赖 musl，不依赖 gcc。

    ★ 但有些子包【真的】需要主包——dash-binsh 的内容是一个指向 dash 的符号
      链接。那类要在配方里显式写 depends，默认值不该替它猜。
    """

    def test_collect_does_not_prepend_parent(self):
        src = (LIB / "collect.py").read_text()
        self.assertNotIn(
            '[pkg["name"]] + [d for d in runtime',
            src,
            "子包的 depends 不该以主包开头——分完之后主包里没有子包的文件",
        )

    def test_subpackage_can_declare_its_own_depends(self):
        src = (LIB / "collect.py").read_text()
        self.assertIn(
            'if "depends" in sub:',
            src,
            '[[subpackage]] 要能自己声明 depends（dash-binsh 那种确实需要主包的）',
        )

    def test_dash_binsh_declares_the_parent(self):
        """反向的例子要留在配方里。

        dash-binsh 提供一个指向 dash 的符号链接，所以它确实依赖主包——这类
        情况就该显式写出来，而不是靠默认值。
        """
        spec = (pathlib.Path(__file__).resolve().parents[2]
                / "recipes" / "tool" / "dash" / "spec.toml").read_text()
        self.assertIn('name        = "dash-binsh"', spec)
        self.assertIn('depends     = ["dash", "glibc"]', spec,
                      "dash-binsh 确实需要主包，要显式声明")


if __name__ == "__main__":
    unittest.main()
