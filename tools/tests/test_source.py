

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
