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

    def test_spec_toml_只改注释不算(self):
        """★ 这个项目的配方注释占大头（openssh 那个 245 行里 180 行是注释）。

        而约定里写着「只改注释不用升 release」—— 按字节哈希的话那句话是假的。
        所以 spec.toml 按语义哈希（tomllib 解析后规范化），注释和排版都不参与。
        """
        a = buildkey.recipe_hash(_配方({"spec.toml": '[package]\nname = "x"\n'}))
        b = buildkey.recipe_hash(_配方({"spec.toml": '# 新加一行注释\n[package]\nname = "x"\n'}))
        self.assertEqual(a, b, "spec.toml 的注释不该进哈希")

    def test_spec_toml_排版也不算(self):
        a = buildkey.recipe_hash(_配方({"spec.toml": '[package]\nname = "x"\nrelease = 1\n'}))
        b = buildkey.recipe_hash(_配方({"spec.toml": '[package]\nname   =   "x"\nrelease=1\n'}))
        self.assertEqual(a, b, "空格对不对齐不该触发重建")

    def test_spec_toml_内容变了要算(self):
        a = buildkey.recipe_hash(_配方({"spec.toml": '[package]\nname = "x"\nversion = "1.0"\n'}))
        b = buildkey.recipe_hash(_配方({"spec.toml": '[package]\nname = "x"\nversion = "1.1"\n'}))
        self.assertNotEqual(a, b, "★ 语义哈希不等于不哈希 —— 内容变了必须变")

    def test_spec_toml_解析不了就退回字节(self):
        """解析不了要退回原始字节，而且要说出来（那条路正常走不到）。"""
        a = buildkey.recipe_hash(_配方({"spec.toml": "这不是 = = toml"}))
        b = buildkey.recipe_hash(_配方({"spec.toml": "这不是 = = toml  # 而注释不同"}))
        self.assertNotEqual(a, b, "解析不了的时候按字节算，保守那头")

    def test_steps_sh_保持字节(self):
        """★ 和 spec.toml 相反：shell 的注释【也】算进去。

        剥 shell 注释要懂 heredoc、引号里的 `#`、续行 —— 剥错的后果是漏掉
        一次真实改动，那比多算几次严重得多。
        """
        a = buildkey.recipe_hash(_配方({"steps.sh": "#!/bin/sh\necho hi\n"}))
        b = buildkey.recipe_hash(_配方({"steps.sh": "#!/bin/sh\n# 注释\necho hi\n"}))
        self.assertNotEqual(a, b, "shell 那边选保守那头，而这是有意的")

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

    def test_打包实现_也算进去(self):
        """★ 这条是补一个真缺口：原来只哈希 skirt/**/*.py。

        `scripts/pack.sh` 决定「包里放什么」（跑 apk mkpkg、恢复能力位、补 ELF
        依赖）—— 改了它产物就变，而 key 不动。**少一样比没有键更危险。**

        ★ 而那件事的形态在 2026-10-15 又变了一次：打包整体搬到了
          `skirtlib/pack.py`（`skirt pack`）。所以现在它由 `skirt/**/*.py`
          覆盖 —— 这条测试钉的是"打包实现必须在这个哈希里"，名字跟着实现走。
        """
        files = sorted(
            p.relative_to(buildkey.ROOT).as_posix()
            for p in list((buildkey.ROOT / "skirt").rglob("*.py"))
            + list((buildkey.ROOT / "scripts").rglob("*.sh"))
            if "__pycache__" not in p.parts
        )
        self.assertIn("skirt/skirtlib/pack.py", files,
                      "打包实现决定产物，它必须在那个哈希里")
        self.assertTrue(any(f.startswith("skirt/") for f in files))
        # scripts/ 那一半也要留在扫描里：keys.sh 是它现在唯一的成员，
        # 而哪天那里再长出会改变产物的脚本，得进这个哈希。
        self.assertTrue(any(f.endswith(".sh") for f in files),
                        f"scripts/*.sh 还该在扫描里，现在只有：{files}")

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
        # ★ 2026-10-21：加了 `toolchain`。而 `buildenv` 仍在（记录用），只是
        #   它不参与计键 —— 判据见 `Test_镜像_Id_不进_build_key`。
        for k in ("arch", "recipe", "epoch", "toolchain", "buildenv", "tooling", "deps"):
            self.assertIn(k, bk["parts"], f"成分里少了 {k} —— 少一样比没有键更危险")
        self.assertEqual(len(bk["parts"]), 7, "成分的数量也要钉住")
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
        # ★ 形状要跟 `compute()` 一致 —— 原来这里还写着 `driver`（那是改名前的
        #   名字），而少了 `toolchain` 就会被"上次那份没有 toolchain"那条判据抓出来。
        parts = {"arch": "x86_64", "recipe": "R", "epoch": "1",
                 "toolchain": "T", "buildenv": "sha256:aaa", "tooling": "D", "deps": {}}
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
        now = self._now(toolchain=buildkey.UNKNOWN)
        need, why = buildkey.needs_rebuild(now, now["parts"])
        self.assertTrue(need, "工具链指纹取不到时必须重建")
        self.assertTrue(any("unknown" in str(w) for w in why))

    def test_上次取不到也重建(self):
        now = self._now()
        before = dict(now["parts"], toolchain=buildkey.UNKNOWN)
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


class Test工具集哈希收哪些文件(unittest.TestCase):
    """★ `skirt/tests/` 不该在里面 —— 2026-10-15 补的。

    测试不参与构建，所以它们改了产物不会变。而原先它们在 39 个文件里占 22 个，
    于是「改一行测试」会让**每个包**的 build_key 都变 → 一次无谓的全量重建。

    ★ 而这条会红的形状要挑对：**改一个测试文件里的代码行**。
      改注释是不会红的（`tooling_hash` 用 ast 把注释和文档字符串剥掉了）——
      所以照"改个注释看看"那样验的话，会验出一个假的"没事"。
    """

    def _收到哪些文件(self):
        import pathlib as _p
        from skirtlib.common import ROOT
        return sorted(
            p for p in list((ROOT / "skirt").rglob("*.py"))
            + list((ROOT / "scripts").rglob("*.sh"))
            if "__pycache__" not in p.parts and "tests" not in p.parts
        )

    def test_测试目录不在里面(self):
        from skirtlib.common import ROOT
        收了 = [p.relative_to(ROOT).as_posix() for p in self._收到哪些文件()]
        混进来的 = [f for f in 收了 if "tests" in f.split("/")]
        self.assertFalse(混进来的, "工具的哈希把测试也算进去了 —— "
                                   "那会让改一行测试触发全量重建：\n  " + "\n  ".join(混进来的))

    def test_驱动本体和打包脚本都在里面(self):
        """反过来那一头：真的参与构建的东西一个都不能漏。"""
        from skirtlib.common import ROOT
        收了 = {p.relative_to(ROOT).as_posix() for p in self._收到哪些文件()}
        for 必须有 in ("skirt/skirtlib/build.py", "skirt/skirtlib/pack.py",
                       "skirt/skirtlib/buildkey.py", "skirt/skirtlib/common.py"):
            self.assertIn(必须有, 收了, f"{必须有} 不在工具集哈希里 —— 改了它产物会变而 key 不动")

    def test_改一个测试文件里的代码不动这个键(self):
        """★ 直接验行为，不只看文件名单。"""
        import pathlib as _p
        import tempfile
        from skirtlib import buildkey
        from skirtlib.common import ROOT

        目标 = None
        for p in (ROOT / "skirt" / "tests").glob("*.py"):
            目标 = p
            break
        self.assertIsNotNone(目标, "找不到测试文件")

        前 = buildkey.tooling_hash()
        原 = 目标.read_text()
        try:
            目标.write_text(原 + "\n_一个新的顶层名字 = 1\n")
            后 = buildkey.tooling_hash()
        finally:
            目标.write_text(原)
        self.assertEqual(前, 后, "改了一个测试文件，工具集哈希变了 —— "
                                 "那会让每个包的 build_key 都变")
        self.assertEqual(前, buildkey.tooling_hash(), "没还原干净")


class Test_产物那条路的模块清单是算出来的(unittest.TestCase):
    """`PAYLOAD_MODULES` 必须是【算出来的】，不是记下来的。

    ★ 2026-10-20：`tooling_hash` 从"哈希 `skirt/**/*.py` 里除 tests 的全部"
      （39 个文件）收窄到"只哈希能改变产物的"（7 个）。动因是一笔实账：那天
      改了 `order.py` 三次，而它不在任何产物的因果链上 —— 三次，每次触发一遍
      全量重建（2 小时）。

    ★★ 而这个方向【危险】：2026-10-15 那次事故就是"哈希少了"
      （`scripts/pack.sh` 没算进去 → 改了它 key 不动、产物变了 → 静默的旧产物）。

      所以这条路要有一个【算出来】的判据钉住：从 `build` / `pack` 出发沿
      import 走一遍闭包，**任何一个能走到而没列进 `PAYLOAD_MODULES` 的模块
      都会让这条测试红**。

      —— 那样"忘了加"就是一次响亮的失败，而不是一次静默的旧产物。
    """

    def _闭包(self):
        import ast
        from pathlib import Path
        d = Path(__file__).resolve().parent.parent / "skirtlib"
        mods = {p.stem: p for p in d.glob("*.py")}

        def 依赖(p):
            t = ast.parse(p.read_text(encoding="utf-8"))
            out = set()
            for n in ast.walk(t):
                if isinstance(n, ast.ImportFrom):
                    if n.level and n.module:            # from .deps import x
                        out.add(n.module.split(".")[0])
                    elif n.level and not n.module:      # from . import x
                        out.update(a.name for a in n.names)
                    elif n.module and n.module.startswith("skirtlib"):
                        out.add(n.module.split(".")[1])
                elif isinstance(n, ast.Import):
                    for a in n.names:
                        if a.name.startswith("skirtlib."):
                            out.add(a.name.split(".")[1])
            return {x for x in out if x in mods}

        seen, stack = set(), ["build", "pack"]
        while stack:
            m = stack.pop()
            if m in seen or m not in mods:
                continue
            seen.add(m)
            stack.extend(依赖(mods[m]))
        return seen

    def test_闭包里的模块一个都不能少(self):
        from skirtlib.buildkey import PAYLOAD_MODULES
        缺 = self._闭包() - set(PAYLOAD_MODULES)
        self.assertEqual(
            缺, set(),
            f"这些模块从 build/pack 出发能走到，而没进 PAYLOAD_MODULES：{sorted(缺)}\n"
            f"      它们的改动会改变产物，而 build_key 不会变 —— 那正是 2026-10-15 "
            f"`scripts/pack.sh` 那次事故的形状。")

    def test_多出来的恰好是那两个显式声明的(self):
        """反面：清单里若有产物那条路走不到、而又【没声明】的模块，改了它会让
        全部包白重建。

        ★ 这半边是 2026-10-20 那次收窄的**目的** —— 它必须真的变窄了，而不是
          只多了一条测试。

        ★★ 而 `deps` / `source` 是【推导不出来】的两个：它们由
          `commands.cmd_build` 在调 `build` 之前用到，而 `commands.py` 里还装着
          restore / deploy / prune 那些和产物无关的东西，所以不能拿它当种子。
          它们进 `PAYLOAD_MODULES_UNDERIVED`，理由写在那边。
        """
        from skirtlib.buildkey import PAYLOAD_MODULES, PAYLOAD_MODULES_UNDERIVED
        多数 = set(PAYLOAD_MODULES) - self._闭包()
        self.assertEqual(
            多数, set(PAYLOAD_MODULES_UNDERIVED),
            f"推导不出来的那些模块：{sorted(多数)}\n"
            f"      而显式声明的只有 {sorted(PAYLOAD_MODULES_UNDERIVED)}。\n"
            f"      要么把它加进 PAYLOAD_MODULES_UNDERIVED 并写清理由，"
            f"要么它根本不该在 PAYLOAD_MODULES 里（改它不改变产物，"
            f"却会让全部包白重建）。")


class Test_镜像_Id_不进_build_key(unittest.TestCase):
    """`buildenv` 只记录、不进键；进键的是 `toolchain` 指纹。

    ★★★ 来历（2026-10-21）：`build_key` 里有一样叫 `buildenv`，值是
      `docker image inspect --format {{.Id}}`。于是**任何一次镜像重建都让整个
      仓库过期** —— 90 个包 × 2 架构 × 2 小时。这一整轮工作里为此跑了三次全量。

      而它其实不该在键里。实测（临时工作流 `probe-toolchain`，用完已删）：
      拿 devel 镜像 commit 出一个「只加了一个标记文件」的副本 —— Id 变了、工具链
      一个字没动 —— 用它俩各建一次 `hello`：

          原件  cc5d4006018b313e8eb22aa6797150b871bf784a8f59e91d11a663fd497c2bd2
          副本  cc5d4006018b313e8eb22aa6797150b871bf784a8f59e91d11a663fd497c2bd2

      **逐字节相同。**

    ★ 而"指纹够不够"有判据：将来若有包在同一指纹下产出不同，说明指纹漏了一样
      东西 —— 那时该补指纹，不是退回镜像 Id。
    """

    def _parts(self, **over):
        d = {"arch": "x86_64", "recipe": "r", "epoch": "1",
             "toolchain": "T1", "tooling": "g", "deps": {}, "buildenv": "sha256:aaa"}
        d.update(over)
        return d

    def test_buildenv_不同而_key_相同(self):
        """★★ 这条就是那 2 小时 × 3 的修复本身。"""
        from skirtlib.buildkey import key_of
        a = key_of(self._parts(buildenv="sha256:aaa"))
        b = key_of(self._parts(buildenv="sha256:bbb"))
        self.assertEqual(a, b, "镜像 Id 不该影响 build_key（实测产物逐字节相同）")

    def test_toolchain_不同而_key_不同(self):
        from skirtlib.buildkey import key_of
        a = key_of(self._parts(toolchain="T1"))
        b = key_of(self._parts(toolchain="T2"))
        self.assertNotEqual(a, b, "工具链变了，产物可能变，键就该变")

    def test_只记录不参与计键的清单(self):
        """清单里只有 `buildenv`。加东西进来要连带更新上面那段实测。"""
        from skirtlib.buildkey import RECORD_ONLY
        self.assertEqual(set(RECORD_ONLY), {"buildenv"})

    def test_取不到工具链时当它变了(self):
        """UNKNOWN 不猜 —— 这是原来的语义，只是判据从 buildenv 挪到了 toolchain。"""
        from skirtlib.buildkey import UNKNOWN, needs_rebuild
        要, 因 = needs_rebuild({"parts": self._parts(toolchain=UNKNOWN)},
                              self._parts(toolchain=UNKNOWN))
        self.assertTrue(要)
        self.assertTrue(any("toolchain" in x for x in 因), 因)

    def test_旧产物没有_toolchain_时也当它变了(self):
        """★ 2026-10-21 之前的 meta.json 里没这一项。**别拿旧结论冒充新结论。**"""
        from skirtlib.buildkey import needs_rebuild
        旧 = self._parts()
        旧.pop("toolchain")
        要, 因 = needs_rebuild({"parts": self._parts()}, 旧)
        self.assertTrue(要)
        self.assertTrue(any("没有 toolchain" in x for x in 因), 因)

    def test_指纹对同一个值稳定(self):
        """`SKIRT_TOOLCHAIN` 是 CI 算一次、所有包共用的，所以必须稳定。"""
        import os
        from unittest import mock
        from skirtlib import buildkey
        with mock.patch.dict(os.environ, {"SKIRT_TOOLCHAIN": "gcc 16.2.0;ld 2.47"}):
            buildkey._TOOLCHAIN_CACHE.clear()
            a = buildkey.toolchain_fingerprint("x86_64")
            b = buildkey.toolchain_fingerprint("x86_64")
        self.assertEqual(a, b)
        self.assertNotEqual(a, buildkey.UNKNOWN)
