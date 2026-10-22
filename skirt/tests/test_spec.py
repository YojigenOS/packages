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
import tomllib
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from skirtlib import build as build_mod
from skirtlib import collect as collect_mod
from skirtlib import spec as spec_mod
from skirtlib.common import RECIPES_DIR as RECIPES
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

class TestLoadSpec(unittest.TestCase):
    def test_missing_package_exits(self):
        with self.assertRaises(SystemExit):
            load_spec("这个包根本不存在")

    def test_real_package(self):
        s = load_spec("zlib")
        self.assertEqual(s["package"]["name"], "zlib")
        self.assertIn("_dir", s, "load_spec 应该把配方目录放进 _dir")

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


class Test每个配方的字段都合法(unittest.TestCase):
    """★ `配方约定.md` 里许诺过这些检查（"测试会验证：…"），而 2026-10-15 核的时候
    一条都不存在 —— 真正会在意它们的地方只有 `source.py` 的下载那一步，也就是
    **要真去建那个包**才走得到。

    所以填错一个 sha256 会一直躺在仓库里，直到某天有人建它、而且建的是填错的那个。
    这一组把它挪到"任何一次跑测试"就能发现的位置。
    """

    def test_sha256_是_64_位十六进制(self):
        import re as _re
        bad = []
        for sp in sorted(RECIPES.rglob("spec.toml")):
            d = tomllib.loads(sp.read_text())
            for s2 in (d.get("source") or []):
                h = s2.get("sha256")
                # ★ 2026-10-22：`sha256` 可以是 {x86_64 = …, aarch64 = …}
                #   （上游按架构分开发布二进制时必需，见 source.py）。
                #   所以逐个值都要是这个格式，而【键也必须是架构名】——
                #   写错一个字（`amd64`）在运行时才会报"没有 x86_64 那一份"。
                if isinstance(h, dict):
                    for k, v in h.items():
                        if k not in ("x86_64", "aarch64"):
                            bad.append(f"{sp.parent.name}: sha256 里有个不认识的架构 {k!r}")
                        if not _re.fullmatch(r"[0-9a-f]{64}", str(v)):
                            bad.append(f"{sp.parent.name}[{k}]: {v!r}")
                elif h and not _re.fullmatch(r"[0-9a-f]{64}", h):
                    bad.append(f"{sp.parent.name}: {h!r}")
        self.assertFalse(bad, "sha256 格式不对（要 64 位小写十六进制）：\n  "
                              + "\n  ".join(bad))

    def test_release_是整数(self):
        bad = []
        for sp in sorted(RECIPES.rglob("spec.toml")):
            d = tomllib.loads(sp.read_text())
            r = (d.get("package") or {}).get("release")
            if r is not None and not isinstance(r, int):
                bad.append(f"{sp.parent.name}: {r!r}")
        self.assertFalse(bad, "release 要是整数（apk 拿它比大小）：\n  " + "\n  ".join(bad))

    def test_url_形式的源必须有_sha256(self):
        """★ 但【git 形式的不需要】—— 而这条我第一版写错了。

        第一版写的是"有源就必须有 sha256"，于是它红了 31 个配方。**而那不是
        配方的问题**：那 31 个用的是 `git = "…" + commit = "…"`,而 `source.py`
        里明写着——

            git 形式【不需要 sha256】……commit hash 就是那棵树的内容哈希，
            比 sha256 更强（sha256 钉的是一份【字节】，而不同托管生成的归档
            字节可以不一样，于是单个 sha256 根本盖不住多个镜像）。

        所以规则是：**`url` 形式要给 sha256，`git` 形式给 commit。**
        """
        import re as _re
        missing, bad_git = [], []
        for sp in sorted(RECIPES.rglob("spec.toml")):
            d = tomllib.loads(sp.read_text())
            for s2 in (d.get("source") or []):
                if "url" in s2 and not s2.get("sha256"):
                    missing.append(sp.parent.name)
                if "git" in s2 and not _re.fullmatch(r"[0-9a-f]{40}", s2.get("commit") or ""):
                    bad_git.append(f"{sp.parent.name}: {s2.get('commit')!r}")
        self.assertFalse(missing, "这些配方用 url 形式的源但没有 sha256：\n  "
                                  + "\n  ".join(missing))
        self.assertFalse(bad_git, "这些配方的 git commit 不是 40 位十六进制：\n  "
                                  + "\n  ".join(bad_git))

    def test_两种源不会混着写(self):
        """一段 `[[source]]` 要么 url+sha256、要么 git+commit。

        混着写的表现是"其中一个字段被静默忽略" —— 而 `source.py` 读的是
        `sources[0]`，所以它只会在真去建那个包的时候才现形。
        """
        bad = []
        for sp in sorted(RECIPES.rglob("spec.toml")):
            d = tomllib.loads(sp.read_text())
            for i, s2 in enumerate(d.get("source") or []):
                has_url, has_git = "url" in s2, "git" in s2
                if has_url == has_git:
                    bad.append(f"{sp.parent.name} 第 {i+1} 段："
                               f"{'两个都有' if has_url else '两个都没有'}")
        self.assertFalse(bad, "这些源的写法混了：\n  " + "\n  ".join(bad))

    def test_没源码的那几个确实没有(self):
        """反过来那一头：`filesystem` / `yojigen-os-init` / `pip` 是自己写的，不该有源。

        ★ 而这一条现在写死了三个名字 —— 加第四个"没源"的包时它会红，
          而那正是该来改这条测试的时候（不是坏事）。
        """
        无源 = {sp.parent.name for sp in RECIPES.rglob("spec.toml")
                if not (tomllib.loads(sp.read_text()).get("source") or [])}
        self.assertEqual(无源, {"filesystem", "yojigen-os-init", "pip"},
                         "没源码的包变了 —— 是新增了一个，还是某个配方的 source 掉了？")


class Test删掉的东西不许长回来(unittest.TestCase):
    """★ `section` 删掉了（决策记录 0021），而这一条守着它别回来。

    它混了两个轴，而两个轴都有别的出处：

        「它是什么」    →  recipes/<类>/ 的目录名（kind_of() 直接读它）
        「它在哪个镜像里」→  distro/variants/<风味>.toml

    而它已经漂过一次：`libs` 17 个里夹着一个 `lib`、`utils` 13 个里夹着一个
    `util`、87 个配方里有两个没有它、而 `section = "base"` 有 14 个配方
    却在 `variants/base.toml` 里是 15 个包 —— **看起来像同一个东西的两个数字**。

    ★ 为什么值得一条测试：`配方约定.md` 里【早就】写过它该删，而 87 个配方
      一个都没改。**一个写在约定里、而没人执行的规矩，会让人以为约定不是真的。**
      所以凡是能写成检查的约定，都配一条会红的。
    """

    def test_没有配方再有_section(self):
        import pathlib as _p
        import tomllib
        from skirtlib.common import RECIPES_DIR
        坏 = []
        for spec in sorted(_p.Path(RECIPES_DIR).rglob("spec.toml")):
            d = tomllib.loads(spec.read_text())
            if "section" in (d.get("package") or {}):
                坏.append(f"{spec.parent.name}（主包）")
            # ★ 子包段是【列表】（`[[subpackage]]`），不是字典 —— 我第一版
            #   按字典写，于是这条测试自己 AttributeError 了。而它之所以能
            #   立刻现形，是因为我跑了一遍。
            for sub in (d.get("subpackage") or []):
                if "section" in sub:
                    坏.append(f"{spec.parent.name}（子包 {sub.get('name', '?')}）")
        self.assertFalse(坏, "这些地方又有 section 了 —— 见决策记录 0021：「一处信息"
                             "只该有一处」，而它那两个轴都已经有出处了：\n  "
                             + "\n  ".join(坏))


class Test_GOARCH_占位符(unittest.TestCase):
    """`$GOARCH` 是 x86_64→amd64、aarch64→arm64。

    ★ 2026-10-22 加，起因是接 docker 插件时撞上的一件事：**同一个 docker 生态里
      两套架构名字并存** ——

          buildx-v0.38.1.linux-amd64       ← Go 的叫法
          docker-compose-linux-x86_64      ← 老叫法

      而 `$ARCH` 只能给一个，所以必须有第二个占位符，不能靠"猜上游用哪个"。

    ★ 而名字取 `$GOARCH` 是因为那套叫法就是从 Go 来的（Go 的 GOARCH），
      不是"我随便起的"——将来看见它的人能立刻知道是 amd64/arm64 那一套。
    """

    def _展开(self, arch):
        from skirtlib.spec import expand
        spec = {"package": {"name": "x", "version": "1"}}
        return expand(spec, "$ARCH|$GOARCH", arch)

    def test_x86_64(self):
        self.assertEqual(self._展开("x86_64"), "x86_64|amd64")

    def test_aarch64(self):
        self.assertEqual(self._展开("aarch64"), "aarch64|arm64")

    def test_不认识架构时展开成空(self):
        """★ 不猜 —— 和 `$ARCH` 在 arch=None 时的行为一致。"""
        self.assertEqual(self._展开(None), "|")
