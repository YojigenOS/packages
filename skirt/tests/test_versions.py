"""配方里的 version 必须是 apk 认的格式。

【为什么单独守着这一条】

实测：sudo 的 version 写成 1.9.17p2（上游的版本号就是这么叫的），
**包建成功了**（二十来分钟的构建），然后在【打包】那一步才报：

    ERROR: info field 'version' has invalid value: package version is invalid
    ERROR: failed to create package: package version is invalid

那一轮其余全绿，只有它红 —— 而红的地方离成因（配方里那一个单词）隔着
整个构建过程。同一轮里如果再有几个包，它们也会一起白跑。

★★ 这和"配方里写错的键会被静默忽略"是同一类的另一面：
   那个是【不报错】，这个是【报得太晚】。
   共同点是：错误的代价乘上了构建时间。

apk 的版本规则（从两个实例反推，都验过）：
    10.5_p1      有效   —— 已发布，装了能跑
    1.9.17p2     无效   —— 打包时被拒
    1.9.17_p2    有效   —— 修法
    20250605     有效   —— 纯数字
    4.99.7       有效   —— 数字和点

也就是：字母后缀后面可以跟数字，但必须先有下划线。
"""

import pathlib
import re
import tomllib
import unittest

RECIPES = pathlib.Path(__file__).resolve().parents[2] / "recipes"


class Test配方里的版本号是apk认的(unittest.TestCase):
    def test_字母后缀跟数字时要先有下划线(self):
        bad = []
        for spec in sorted(RECIPES.glob("*/*/spec.toml")):
            d = tomllib.load(open(spec, "rb"))
            name = d["package"]["name"]
            ver = d["package"]["version"]
            # 找 <字母><数字> 且不是 <_><字母><数字> 的地方
            for m in re.finditer(r"[A-Za-z]([0-9]+)", ver):
                if m.start() > 0 and ver[m.start() - 1] == "_":
                    continue
                bad.append(f"{name}  version = {ver!r}  —— 第 {m.start()+1} 位 "
                           f"({m.group(0)!r}) 字母后直接跟数字，apk 不认。"
                           f"要么写成 _{m.group(0)}，要么别用那个后缀")
                break
        self.assertFalse(
            bad,
            "apk 的版本格式里，字母后缀后面跟数字必须先有下划线：\n         "
            + "\n         ".join(bad)
            + "\n         （这个错现在会在【打包】那步才报，而那时包已经建了二十来分钟）")

    def test_版本号里没有apk的保留字符(self):
        bad = []
        for spec in sorted(RECIPES.glob("*/*/spec.toml")):
            d = tomllib.load(open(spec, "rb"))
            ver = d["package"]["version"]
            for ch in ver:
                if not (ch.isalnum() or ch in "._+-"):
                    bad.append(f"{d['package']['name']}  version = {ver!r} 含 {ch!r}")
                    break
        self.assertFalse(bad, "版本号里有不该有的字符：\n         " + "\n         ".join(bad))


if __name__ == "__main__":
    unittest.main()
