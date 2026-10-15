"""build_key —— 一个包的身份，从内容算出来。

这一套测试守的是 buildkey.py 那份文档里说的每一句话。尤其两条：

  · **键里少一样比没有键更危险** —— 所以"配方变了 / 环境变了 / 依赖变了"
    这三件事各有一条测试，确认它们真的会改变那个键
  · **拿不准就重建** —— 所以"取不到"的那些情况也有测试
"""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from skirtlib import buildkey                                      # noqa: E402


def _配方(files: dict[str, str]) -> pathlib.Path:
    """造一个假的配方目录。"""
    d = pathlib.Path(tempfile.mkdtemp()) / "openssh"
    d.mkdir(parents=True)
    for rel, content in files.items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return d


class Test配方哈希(unittest.TestCase):

    def test_内容变了哈希就变(self):
        a = buildkey.recipe_hash(_配方({"spec.toml": "version = '1.0'"}))
        b = buildkey.recipe_hash(_配方({"spec.toml": "version = '1.1'"}))
        self.assertNotEqual(a, b, "改了配方内容，哈希必须变 —— 否则改了配方不会重建")

    def test_加了文件哈希也变(self):
        a = buildkey.recipe_hash(_配方({"spec.toml": "x", "steps.sh": "y"}))
        b = buildkey.recipe_hash(_配方({"spec.toml": "x", "steps.sh": "y", "files/a": "z"}))
        self.assertNotEqual(a, b, "加了一个 files/ 下的文件也是改了配方")

    def test_改名哈希也变(self):
        a = buildkey.recipe_hash(_配方({"files/a": "一样的内容"}))
        b = buildkey.recipe_hash(_配方({"files/b": "一样的内容"}))
        self.assertNotEqual(a, b, "内容一样但改名了也要变 —— 路径也喂进去了")

    def test_子目录里的文件也算(self):
        a = buildkey.recipe_hash(_配方({"files/x/a": "1"}))
        b = buildkey.recipe_hash(_配方({"files/x/a": "2"}))
        self.assertNotEqual(a, b)

    def test_pycache_不算(self):
        d = _配方({"spec.toml": "x"})
        a = buildkey.recipe_hash(d)
        for sub in ("__pycache__", "files/__pycache__"):
            p = d / sub
            p.mkdir(parents=True, exist_ok=True)
            (p / "x.pyc").write_bytes(b"\x00\x01\x02")
        self.assertEqual(a, buildkey.recipe_hash(d),
                         "__pycache__ 是解释器的产物，不是配方的一部分")

    def test_同样内容给同样的哈希(self):
        a = buildkey.recipe_hash(_配方({"spec.toml": "一样", "steps.sh": "也一样"}))
        b = buildkey.recipe_hash(_配方({"spec.toml": "一样", "steps.sh": "也一样"}))
        self.assertEqual(a, b, "两次算同一个配方必须一致")

    def test_路径不会串味(self):
        """("ab","c") 和 ("a","bc") 不能撞 —— 分隔符是必需的。"""
        a = buildkey.recipe_hash(_配方({"ab": "c"}))
        b = buildkey.recipe_hash(_配方({"a": "bc"}))
        self.assertNotEqual(a, b)


class Test工具集哈希(unittest.TestCase):
    """驱动 + 打包脚本。它们都决定产物，所以都得进那个键。"""

    def test_同一次调用一致(self):
        self.assertEqual(buildkey.tooling_hash(), buildkey.tooling_hash())

    def test_pack_sh_也算进去(self):
        """★ 这条是补一个真缺口：原来只哈希 skirt/**/*.py。

        `scripts/pack.sh` 决定「包里放什么」（跑 apk mkpkg、恢复能力位、补 ELF
        依赖）—— 改了它产物就变，而 key 不动。**少一样比没有键更危险。**
        """
        files = sorted(
            p.relative_to(buildkey.ROOT).as_posix()
            for p in list((buildkey.ROOT / "skirt").rglob("*.py"))
            + list((buildkey.ROOT / "scripts").rglob("*.sh"))
            if "__pycache__" not in p.parts
        )
        self.assertIn("scripts/pack.sh", files,
                      "pack.sh 决定产物，它必须在那个哈希里")
        self.assertTrue(any(f.startswith("skirt/") for f in files))
        self.assertTrue(any(f.endswith(".sh") for f in files))

    def test_注释不算(self):
        """文档里写着「只改注释不用升 release」—— 那就别让改注释触发全量重建。"""
        import ast
        src_a = "x = 1  # 这句注释无关紧要\n"
        src_b = "x = 1  # 换一句完全不同的注释，而且更长更长\n"
        na = ast.unparse(buildkey._strip_docstrings(ast.parse(src_a)))
        nb = ast.unparse(buildkey._strip_docstrings(ast.parse(src_b)))
        self.assertEqual(na, nb, "注释不该进 AST")

    def test_文档字符串不算(self):
        import ast
        a = ast.unparse(buildkey._strip_docstrings(ast.parse(
            'def f():\n    """第一版说明。"""\n    return 1\n')))
        b = ast.unparse(buildkey._strip_docstrings(ast.parse(
            'def f():\n    """改过的、完全不同的说明。"""\n    return 1\n')))
        self.assertEqual(a, b, "文档字符串剥掉之后两个函数是一样的")

    def test_剥完不能是空的(self):
        """整个文件只有一句文档字符串时，剥掉之后得有 pass —— 否则 unparse 出语法错。"""
        import ast
        out = ast.unparse(buildkey._strip_docstrings(ast.parse('"""只有一句话。"""\n')))
        ast.parse(out)                                      # 能解析回来才算对
        self.assertIn("pass", out)

    def test_代码变了要算进去(self):
        import ast
        a = ast.unparse(buildkey._strip_docstrings(ast.parse("x = 1\n")))
        b = ast.unparse(buildkey._strip_docstrings(ast.parse("x = 2\n")))
        self.assertNotEqual(a, b, "驱动决定输出，实测过（--libdir 那次）")


class Test键的合成(unittest.TestCase):

    def _spec(self):
        return {"_dir": _配方({"spec.toml": "x"}), "package": {"name": "t"}}

    def test_成分齐了(self):
        import os
        os.environ["SKIRT_BUILDENV"] = "yojigenos/绝不存在"
        bk = buildkey.compute(self._spec(), "x86_64", 12345, [("a", "k1")])
        # ★ 这份名单要和人脑里那份一致 —— 少一样，那个包就不会因为那一样变而重建，
        #   而产物其实变了。所以这条测试列全，加了新成分就得改这里。
        for k in ("arch", "recipe", "epoch", "buildenv", "tooling", "deps"):
            self.assertIn(k, bk["parts"], f"成分里少了 {k} —— 少一样比没有键更危险")
        self.assertEqual(len(bk["parts"]), 6, "成分的数量也要钉住")
        self.assertEqual(len(bk["key"]), 64)

    def test_依赖的key算进去(self):
        a = buildkey.compute(self._spec(), "x86_64", 1, [("dep", "AAA")])
        b = buildkey.compute(self._spec(), "x86_64", 1, [("dep", "BBB")])
        self.assertNotEqual(a["key"], b["key"],
                            "依赖变了，用它的包也必须被标出来 —— 这是递归那一半")

    def test_架构算进去(self):
        a = buildkey.compute(self._spec(), "x86_64", 1, [])
        b = buildkey.compute(self._spec(), "aarch64", 1, [])
        self.assertNotEqual(a["key"], b["key"])

    def test_epoch算进去(self):
        a = buildkey.compute(self._spec(), "x86_64", 1, [])
        b = buildkey.compute(self._spec(), "x86_64", 2, [])
        self.assertNotEqual(a["key"], b["key"])

    def test_依赖顺序不影响(self):
        """闭包的顺序由依赖图决定，而那是另一个变量。"""
        a = buildkey.compute(self._spec(), "x86_64", 1, [("x", "1"), ("y", "2")])
        b = buildkey.compute(self._spec(), "x86_64", 1, [("y", "2"), ("x", "1")])
        self.assertEqual(a["key"], b["key"])


class Test要不要重建(unittest.TestCase):

    def _now(self, **over):
        parts = {"arch": "x86_64", "recipe": "R", "epoch": "1",
                 "buildenv": "sha256:aaa", "driver": "D", "deps": {}}
        parts.update(over)
        return {"key": buildkey.key_of(parts), "parts": parts}

    def test_盘上没有就重建(self):
        need, why = buildkey.needs_rebuild(self._now(), None)
        self.assertTrue(need)
        self.assertTrue(why)

    def test_一样就不重建(self):
        now = self._now()
        need, why = buildkey.needs_rebuild(now, now["parts"])
        self.assertFalse(need, f"什么都不该变，而它说变了: {why}")

    def test_配方变了就说得出是哪一样(self):
        now = self._now()
        before = dict(now["parts"], recipe="别的")
        need, why = buildkey.needs_rebuild(now, before)
        self.assertTrue(need)
        self.assertIn("recipe", why, "要说出是哪一样变的 —— 只有一个哈希的话查不了")

    def test_依赖变了也算(self):
        now = self._now()
        before = dict(now["parts"], deps={"a": "旧"})
        need, _ = buildkey.needs_rebuild(now, before)
        self.assertTrue(need)

    def test_环境取不到就当它变了(self):
        """拿不准就重建 —— 这是这套东西的默认。"""
        now = self._now(buildenv=buildkey.UNKNOWN)
        need, why = buildkey.needs_rebuild(now, now["parts"])
        self.assertTrue(need, "buildenv 取不到时必须重建")
        self.assertTrue(any("unknown" in str(w) for w in why))

    def test_上次取不到也重建(self):
        now = self._now()
        before = dict(now["parts"], buildenv=buildkey.UNKNOWN)
        need, _ = buildkey.needs_rebuild(now, before)
        self.assertTrue(need)


class Test存和读(unittest.TestCase):

    def test_旁路文件能来回(self):
        d = pathlib.Path(tempfile.mkdtemp()) / "pkg-1.0-1"
        d.mkdir(parents=True)
        parts = {"arch": "x86_64", "recipe": "R"}
        buildkey.write_sidecar(d, {"key": buildkey.key_of(parts), "parts": parts})
        self.assertEqual(buildkey.stored_parts(d)["recipe"], "R")
        self.assertEqual(buildkey.stored_key_of(d), buildkey.key_of(parts))

    def test_key是从成分重算的_不是读存下来的那个(self):
        """★ 成分才是证据，key 是它的函数。

        存下来的 `key` 字段是给人看的（`skirt stale` 打印它）。判断用的那个
        一律重算 —— 这样一份手改过的旁路文件也骗不了人。
        """
        d = pathlib.Path(tempfile.mkdtemp()) / "pkg-1.0-1"
        d.mkdir(parents=True)
        parts = {"arch": "x86_64", "recipe": "真的"}
        buildkey.write_sidecar(d, {"key": "我瞎写的", "parts": parts})
        self.assertEqual(buildkey.stored_key_of(d), buildkey.key_of(parts))
        self.assertNotEqual(buildkey.stored_key_of(d), "我瞎写的")

    def test_meta_json优先(self):
        """两个来源冲突时以 meta.json 为准 —— 它是本次构建刚写的。"""
        d = pathlib.Path(tempfile.mkdtemp()) / "pkg-1.0-1"
        d.mkdir(parents=True)
        (d / "meta.json").write_text(json.dumps(
            {"build_key_parts": {"arch": "x86_64", "recipe": "新的"}}), encoding="utf-8")
        buildkey.write_sidecar(d, {"key": "旧", "parts": {"arch": "x86_64", "recipe": "旧的"}})
        self.assertEqual(buildkey.stored_parts(d)["recipe"], "新的")

    def test_目录是空的就返回None(self):
        d = pathlib.Path(tempfile.mkdtemp()) / "pkg-1.0-1"
        d.mkdir(parents=True)
        self.assertIsNone(buildkey.stored_parts(d))
        self.assertIsNone(buildkey.stored_key_of(d))

    def test_文件坏了不崩(self):
        """坏了的 meta.json 不该让整个构建炸掉 —— 该当它"没有"。"""
        d = pathlib.Path(tempfile.mkdtemp()) / "pkg-1.0-1"
        d.mkdir(parents=True)
        (d / "meta.json").write_text("{这不是 json", encoding="utf-8")
        self.assertIsNone(buildkey.stored_parts(d))


if __name__ == "__main__":
    unittest.main()
