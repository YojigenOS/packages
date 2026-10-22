#!/usr/bin/env python3
"""源码获取的测试。

重点在 srcrel_of：它只读 tar 里的名字，不把文件树物化到宿主上。
宿主是 macOS，APFS 默认大小写不敏感，而内核源码里有 13 组只差大小写的
文件——物化就会互相覆盖。见那个函数自己的注释。
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class TestSrcrelOnlyReadsNames(unittest.TestCase):
    """srcrel_of 只能读 tar 成员名，不能把文件树物化到宿主上。

    【为什么守着这一条】宿主是 macOS，APFS 默认大小写不敏感。内核源码里有
    13 组只差大小写的文件：

        include/uapi/linux/netfilter/xt_MARK.h  /  xt_mark.h
        net/netfilter/xt_TCPMSS.c               /  xt_tcpmss.c
        …（tarball 里 13 组）

    在宿主上解压，13 组会变成 0 组——大写那个把小写那个覆盖掉。而它们不是
    重复文件：`MARK` 是给包打标记、`mark` 是匹配那个标记。于是 xt_mark.c
    找不到它 include 的 xt_mark.h，构建在 47 分钟后失败，报的是「找不到
    xt_mark.h」这种指不到根因的错误。

    读名字没有这个问题：名字只是字符串。
    """

    def _tar(self, entries):
        import tarfile, tempfile, io
        f = tempfile.NamedTemporaryFile(suffix=".tar", delete=False)
        with tarfile.open(fileobj=f, mode="w") as tf:
            for name in entries:
                info = tarfile.TarInfo(name)
                info.size = 0
                tf.addfile(info, io.BytesIO(b""))
        f.close()
        return Path(f.name)

    def test_只有一个顶层目录时返回它(self):
        from skirtlib.source import srcrel_of
        t = self._tar(["pkg-1.0/README", "pkg-1.0/src/a.c"])
        self.assertEqual(srcrel_of(t), "pkg-1.0")

    def test_散在根目录时返回点(self):
        from skirtlib.source import srcrel_of
        t = self._tar(["README", "a.c"])
        self.assertEqual(srcrel_of(t), ".")

    def test_单个顶层文件不算目录(self):
        from skirtlib.source import srcrel_of
        t = self._tar(["README"])
        self.assertEqual(srcrel_of(t), ".")

    def test_只差大小写的成员名都能读到(self):
        """这条才是重点：读名字不会丢东西。"""
        from skirtlib.source import srcrel_of
        t = self._tar([
            "pkg-1.0/include/uapi/linux/netfilter/xt_MARK.h",
            "pkg-1.0/include/uapi/linux/netfilter/xt_mark.h",
            "pkg-1.0/net/netfilter/xt_TCPMSS.c",
            "pkg-1.0/net/netfilter/xt_tcpmss.c",
        ])
        import tarfile
        with tarfile.open(t) as tf:
            names = tf.getnames()
        self.assertIn("pkg-1.0/include/uapi/linux/netfilter/xt_MARK.h", names)
        self.assertIn("pkg-1.0/include/uapi/linux/netfilter/xt_mark.h", names)
        self.assertEqual(srcrel_of(t), "pkg-1.0")


class Test_sha256_按架构给(unittest.TestCase):
    """`sha256` 可以是 `{x86_64 = "…", aarch64 = "…"}`。

    ★★★ 来历（2026-10-22，接 docker 的时候）：决策记录 0022/0023 那条路的
      常态是**分发上游自己的二进制**，而它们几乎总是**按架构分开发布**的：

          …/static/stable/x86_64/docker-29.9.2.tgz    86 MB
          …/static/stable/aarch64/docker-29.9.2.tgz   78 MB

      两个文件不同，摘要当然也不同。而原来的 `sha256` 只能是【一个字符串】,
      所以这种包根本写不出来。

    ★ 而"缺当前架构那一份"必须【直接报错】，不猜、不退回另一个架构的值 ——
      退回会让 aarch64 拿 x86_64 的摘要去校验，那必然失败，而报出来的是
      「校验不通过」，指向不了真正的原因。
    """

    def _spec(self, sha):
        return {"package": {"name": "probe", "version": "1.0"}, "_dir": Path("/tmp"),
                "source": [{"url": "https://example.invalid/d-$ARCH.tgz",
                            "sha256": sha}],
                "dependencies": {"x86_64": {"runtime": [], "build": []},
                                 "aarch64": {"runtime": [], "build": []}}}

    def test_两个架构各用各的摘要(self):
        """★ 正向：按 URL 里的架构给不同的摘要，两边都该通过。"""
        from unittest import mock
        from tempfile import TemporaryDirectory
        from skirtlib import source

        for arch, 该用的 in (("x86_64", "a" * 64), ("aarch64", "b" * 64)):
            with TemporaryDirectory() as d:
                d = Path(d)
                with mock.patch.object(source, "_scratch",
                                       lambda n, d=d: d / n), \
                     mock.patch.object(source, "_download",
                                       lambda url, tmp: tmp.write_bytes(b"x")), \
                     mock.patch.object(source, "sha256_of", lambda p: 该用的):
                    spec = self._spec({"x86_64": "a" * 64, "aarch64": "b" * 64})
                    得 = source.fetch(spec, arch)
                    self.assertEqual(得.name, f"d-{arch}.tgz")

    def test_缺当前架构那一份就报错(self):
        """★★ 这条比"能取到"更重要 —— 它是那个"不猜"的判据。"""
        import contextlib, io
        from skirtlib import source
        输出 = io.StringIO()
        with contextlib.redirect_stderr(输出), self.assertRaises(SystemExit):
            source.fetch(self._spec({"x86_64": "a" * 64}), "aarch64")
        说明 = 输出.getvalue()
        self.assertIn("没有 aarch64 那一份", 说明)
        self.assertIn("x86_64", 说明, "要说清写了哪些")

    def test_字符串写法照旧(self):
        """★ 别修坏老写法：90 个配方全是字符串。"""
        import contextlib, io
        from skirtlib import source
        输出 = io.StringIO()
        with contextlib.redirect_stderr(输出), self.assertRaises(SystemExit):
            source.fetch(self._spec("c" * 64), "x86_64")
        说明 = 输出.getvalue()
        self.assertNotIn("那一份", 说明)
        # 摘要那一关过了，才会走到下载并失败（域名是 .invalid）
        self.assertIn("所有源都取不到", 说明)
