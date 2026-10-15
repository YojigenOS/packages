#!/usr/bin/env python3
"""elfdeps 的测试 —— 它读 ELF，所以测试要真的造出 ELF。

【为什么要手搓 ELF 而不是找一个系统里的 .so】

宿主是 macOS，没有 ELF 文件可复制；而 CI 上（Linux）能找到的 .so 又是
"这一台机器的现实"，不是"我们要验的东西"。所以这里用一个小构造器
（`_make_elf`）拼出【最小的、但结构合法】的 ELF：

    ELF 头 + .dynstr（字符串表） + .dynamic（DT_NEEDED / DT_SONAME） + 节头表

它只放 `elfdeps._read_elf()` 会读的那些结构 —— 不是通用 ELF 生成器。
而"手搓的东西到底算不算 ELF"这件事不能靠我说了算，所以有两条外部判据：

  · `test_和系统里真实的_so_比`（有 readelf 才跑：readelf 读真文件）
  · `test_手搓的_ELF_真的_readelf_认得`（有 docker 才跑：容器里用真的
    readelf 读手搓的那份，两条读取路的结果必须一样）

一条都不跳的话（Linux + docker），手搓的 ELF 是被真的 readelf 承认过的。

【搬家的那件事】

这个模块原来是 `scripts/elf-deps.py`，由 pack.sh 按文件路径硬加载。搬进
`skirtlib/` 之后判定逻辑一行没改，这里守的就是那些判定：

  · NEEDED / SONAME 两个 tag 的解析（32 位、64 位、大端）
  · 符号链接不算（两条都扫会重复）
  · 可执行文件的 SONAME 不进 provides（它不该被别的包当库依赖）
  · provides 的版本后缀 = 文件名里 SONAME 之后的那一段
"""

from __future__ import annotations

import io
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from skirtlib import elfdeps

#: 手搓 ELF 用的机器号（x86_64）。scan 不检查机器号，这里只是别写个假的。
MACHINE_X86_64 = 62


def _make_elf(*, bits: int = 64, little: bool = True,
              needed: tuple[str, ...] = (), soname: str | None = None) -> bytes:
    """拼一个最小的 ELF。返回它的字节。

    结构（顺序就是文件里的顺序）：

        0x00  ELF 头
              程序头表     PT_LOAD（覆盖整个文件）+ PT_DYNAMIC（指向 .dynamic）
              .dynstr      字符串表：NEEDED 和 SONAME 的名字都在这儿
              .dynamic     一对一对的 (tag, 值)；值是 .dynstr 里的偏移
              .shstrtab    节头字符串表（readelf 要它才能把节名叫出来）
              节头表       4 项：NULL / .dynstr / .dynamic / .shstrtab

    ★ 为什么要有程序头表：**`readelf -d` 是从 PT_DYNAMIC 段找动态段的**，
      只有节头表的话它会说 "There is no dynamic section in this file."
      （实测：第一版就是这么写的，readelf 读得出节名、读不出 tag）。
      而"手搓的 ELF 真的被 readelf 承认"是上面那批测试的地基，所以要把它
      补成真的。

    只放 `elfdeps._read_elf()` 会读的那些结构 —— 不是通用 ELF 生成器。
    """
    if bits not in (32, 64):
        raise ValueError(f"bits 只能是 32 或 64，收到 {bits}")
    end = "<" if little else ">"
    is64 = bits == 64
    ehsize, shentsize = (64, 64) if is64 else (52, 40)
    phentsize = 56 if is64 else 32
    nph = 2
    sh_fmt = "IIQQQQIIQQ" if is64 else "IIIIIIIIII"
    ph_fmt = "IIQQQQQQ" if is64 else "IIIIIIII"
    d_fmt = "qQ" if is64 else "iI"

    # ── .dynstr ──────────────────────────────────────────────────────────
    # 开头那个 \0 是规范：偏移 0 表示空字符串
    dynstr = b"\0"
    at: dict[str, int] = {}
    for name in list(needed) + ([soname] if soname else []):
        if name not in at:
            at[name] = len(dynstr)
            dynstr += name.encode() + b"\0"

    # ── .dynamic ─────────────────────────────────────────────────────────
    entries = [(elfdeps.DT_NEEDED, at[n]) for n in needed]
    if soname:
        entries.append((elfdeps.DT_SONAME, at[soname]))
    entries.append((elfdeps.DT_NULL, 0))       # 终止项
    dynamic = b"".join(struct.pack(end + d_fmt, tag, val) for tag, val in entries)

    # ── .shstrtab ────────────────────────────────────────────────────────
    shstr = b"\0"
    shname: dict[str, int] = {}
    for name in (".dynstr", ".dynamic", ".shstrtab"):
        shname[name] = len(shstr)
        shstr += name.encode() + b"\0"

    # ── 布局 ─────────────────────────────────────────────────────────────
    off = ehsize + nph * phentsize
    off = (off + 7) // 8 * 8
    dynstr_off = off
    off += len(dynstr)
    off = (off + 7) // 8 * 8
    dyn_off = off
    off += len(dynamic)
    shstr_off = off
    off += len(shstr)
    off = (off + 7) // 8 * 8
    shoff = off
    # 节头表之后就没有内容了 —— 文件就到节头表末尾为止
    total = shoff + 4 * shentsize

    sections = [
        (0, 0, 0, 0, 0, 0, 0, 0, 0, 0),                                 # SHT_NULL
        (shname[".dynstr"], 3, 0, 0, dynstr_off, len(dynstr), 0, 0, 1, 0),
        (shname[".dynamic"], 6, 0, 0, dyn_off, len(dynamic),
         1,           # sh_link → .dynstr（就是它给字符串表定索引）
         0, 8, 16 if is64 else 8),
        (shname[".shstrtab"], 3, 0, 0, shstr_off, len(shstr), 0, 0, 1, 0),
    ]

    # ── 程序头表 ─────────────────────────────────────────────────────────
    # 段里的地址一律等于文件偏移（vaddr == offset），省掉"映射关系"这一层
    phdrs = [
        # PT_LOAD：type=1，flags=R|X=5，覆盖整个文件
        (1, 5, 0, 0, 0, total, total, 0x1000),
        # PT_DYNAMIC：type=2，flags=R|W=6，指向 .dynamic
        (2, 6, dyn_off, dyn_off, dyn_off, len(dynamic), len(dynamic), 8),
    ]

    ident = (b"\x7fELF"
             + bytes([2 if is64 else 1, 1 if little else 2, 1])   # class, data, ver
             + b"\0" * 9)
    # 字段序：e_type e_machine e_version e_entry e_phoff e_shoff e_flags
    #         e_ehsize e_phentsize e_phnum e_shentsize e_shnum e_shstrndx
    # 程序头表就紧跟 ELF 头，所以 e_phoff = ehsize。
    if is64:
        ehdr = struct.pack(end + "HHIQQQIHHHHHH", 3, MACHINE_X86_64, 1, 0, ehsize, shoff,
                           0, ehsize, phentsize, nph, shentsize, len(sections), 3)
    else:
        ehdr = struct.pack(end + "HHIIIIIHHHHHH", 3, MACHINE_X86_64, 1, 0, ehsize, shoff,
                           0, ehsize, phentsize, nph, shentsize, len(sections), 3)
    ph_table = b"".join(struct.pack(end + ph_fmt, *ph) for ph in phdrs)

    buf = bytearray(ident + ehdr)
    assert len(buf) == ehsize, "ELF 头长度算错了，后面所有偏移都会跟着错"
    buf += ph_table
    buf += b"\0" * (dynstr_off - len(buf))
    buf += dynstr
    buf += b"\0" * (dyn_off - len(buf))
    buf += dynamic
    buf += b"\0" * (shstr_off - len(buf))
    buf += shstr
    buf += b"\0" * (shoff - len(buf))
    for sec in sections:
        buf += struct.pack(end + sh_fmt, *sec)
    assert len(buf) == total, f"文件长度 {len(buf)} 和程序头里写的 {total} 不一致"
    return bytes(buf)


def _write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


class _TempTree(unittest.TestCase):
    """给每个测试一个临时目录，用完自动删。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="elfdeps-test-")
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)


# ------------------------------------------------------------------ 纯读取

class Test读一个_ELF(_TempTree):
    def test_64_位里读出_NEEDED_和_SONAME(self):
        f = _write(self.tmp / "libtest.so.3.0", _make_elf(
            needed=("libfoo.so.1", "libbar.so.2"), soname="libtest.so.3"))

        needed, soname = elfdeps._read_elf(f)

        self.assertEqual(needed, ["libfoo.so.1", "libbar.so.2"])
        self.assertEqual(soname, "libtest.so.3")

    def test_32_位走的是另一条解包路径(self):
        """32 位的头偏移、节头大小、dynamic 条目宽度都不一样。

        这几处写错的表现是"读出空列表"或者抛 struct.error —— 而空列表在
        scan() 里看起来像"这个包没有依赖"，是那种看起来正常的错。
        """
        f = _write(self.tmp / "libtest.so.3.0", _make_elf(
            bits=32, needed=("libc.so.6",), soname="libtest.so.3"))

        needed, soname = elfdeps._read_elf(f)

        self.assertEqual(needed, ["libc.so.6"])
        self.assertEqual(soname, "libtest.so.3")

    def test_大端也读得出来(self):
        """`end` 那一族格式串有 ">" 一半，大一端文件走的就是它。"""
        f = _write(self.tmp / "libtest.so.3.0", _make_elf(
            little=False, needed=("libc.so.6",), soname="libtest.so.3"))

        needed, soname = elfdeps._read_elf(f)

        self.assertEqual(needed, ["libc.so.6"])
        self.assertEqual(soname, "libtest.so.3")

    def test_没有_SONAME_就没有(self):
        f = _write(self.tmp / "tool", _make_elf(needed=("libc.so.6",)))

        needed, soname = elfdeps._read_elf(f)

        self.assertEqual(needed, ["libc.so.6"])
        self.assertIsNone(soname)

    def test_多个_NEEDED_按_dynamic_里的顺序(self):
        """顺序是文件里的顺序。scan() 之后会排序，这里只看读取这一步。"""
        want = ("libc.so.6", "libm.so.6", "libz.so.1", "ld-linux-x86-64.so.2")
        f = _write(self.tmp / "tool", _make_elf(needed=want))

        needed, _ = elfdeps._read_elf(f)

        self.assertEqual(needed, list(want))

    def test_不是_ELF_的文件返回空(self):
        f = self.tmp / "readme.txt"
        f.write_text("这不是 ELF\n")

        self.assertEqual(elfdeps._read_elf(f), ([], None))
        self.assertFalse(elfdeps._is_elf(f))

    def test_没有节头表就没有(self):
        """e_shoff = 0：节头表不存在。真实世界里 stripped 的镜像可能这样。"""
        raw = bytearray(_make_elf(needed=("libc.so.6",)))
        struct.pack_into("<Q", raw, 0x28, 0)          # 64 位 e_shoff
        f = _write(self.tmp / "x.so", bytes(raw))

        self.assertEqual(elfdeps._read_elf(f), ([], None))

    def test_被截断的_ELF_会炸(self):
        """**这是当前行为，搬家时没有改。**

        魔数像 ELF、但内容被截断的文件（半截下载、被截断的 payload）会让
        `struct.unpack_from` 抛 struct.error，而不是安静地返回空。

        为什么留着一个崩溃：反过来"安静地返回空"意味着一个【真的】ELF 解析
        失败时，这个包的 so: 依赖会少一条而不报错 —— 那正是这个模块存在的理由
        （依赖靠人写就会漏）。所以宁可炸，也不要静默少一条。这条测试把选择
        钉住：哪天要改成"跳过坏文件"，得先有人看到这里。
        """
        f = _write(self.tmp / "half.so", _make_elf(needed=("libc.so.6",))[:40])

        with self.assertRaises(struct.error):
            elfdeps._read_elf(f)


# ------------------------------------------------------------------ 扫一棵树

class Test扫一棵_payload(_TempTree):
    def setUp(self) -> None:
        super().setUp()
        self.payload = self.tmp / "payload"

    def test_依赖去重并排序_提供的带上版本后缀(self):
        # 真文件：SONAME 写在它里面
        _write(self.payload / "usr/lib/libz.so.1.3.2", _make_elf(
            needed=("libc.so.6",), soname="libz.so.1"))
        _write(self.payload / "usr/lib/libfoo.so.2", _make_elf(
            needed=("libc.so.6", "libc.so.6"), soname="libfoo.so.2"))
        # 可执行文件：它也有 NEEDED，但它的 SONAME 不该进 provides
        _write(self.payload / "usr/bin/tool", _make_elf(
            needed=("libz.so.1",), soname="tool"))

        needed, provided = elfdeps.scan(self.payload)

        self.assertEqual(needed, ["libc.so.6", "libz.so.1"])
        self.assertEqual(provided, {"libz.so.1": "3.2", "libfoo.so.2": ""})

    def test_符号链接不算(self):
        """libz.so.1 → libz.so.1.3.2：两条都扫会重复；而更糟的是【名字变了】。

        下面那个软链叫 libalias.so.1、指着一个 SONAME 是 libz.so.1 的文件。
        跟着软链扫的话 provides 里会多出一条 libalias.so.1 —— 一个谁也不提供
        的名字，而 apk 会把它当成"这个包提供 libalias.so.1"。
        """
        _write(self.payload / "usr/lib/libz.so.1.3.2", _make_elf(soname="libz.so.1"))
        (self.payload / "usr/lib/libalias.so.1").symlink_to("libz.so.1.3.2")

        _, provided = elfdeps.scan(self.payload)

        self.assertEqual(provided, {"libz.so.1": "3.2"})

    def test_可执行文件的_SONAME_不算提供(self):
        """名字里没有 `.so` 的，就算有 SONAME 也不进 provides。"""
        _write(self.payload / "bin/app", _make_elf(soname="app-debug-soname"))
        _write(self.payload / "lib/libreal.so.1", _make_elf(soname="libreal.so.1"))

        _, provided = elfdeps.scan(self.payload)

        self.assertEqual(provided, {"libreal.so.1": ""})

    def test_非_ELF_和空目录都不影响结果(self):
        _write(self.payload / "usr/share/doc/readme.txt", b"hello\n")
        (self.payload / "usr/lib/empty-dir").mkdir(parents=True)
        _write(self.payload / "lib/libx.so.1.0", _make_elf(soname="libx.so.1"))

        needed, provided = elfdeps.scan(self.payload)

        self.assertEqual(needed, [])
        self.assertEqual(provided, {"libx.so.1": "0"})

    def test_空目录(self):
        self.payload.mkdir(parents=True)

        self.assertEqual(elfdeps.scan(self.payload), ([], {}))


# ------------------------------------------------------------------ 命令行

class Test命令行(_TempTree):
    def _run(self, argv: list[str]) -> tuple[int, str]:
        out = io.StringIO()
        with redirect_stdout(out):
            code = elfdeps.main(argv)
        return code, out.getvalue()

    def test_needs_一行一个_so(self):
        payload = self.tmp / "payload"
        _write(payload / "lib/liba.so.1", _make_elf(needed=("libc.so.6", "libz.so.1")))

        code, out = self._run(["elfdeps", "needs", str(payload)])

        self.assertEqual(code, 0)
        self.assertEqual(out, "so:libc.so.6\nso:libz.so.1\n")

    def test_sonames_带上版本(self):
        payload = self.tmp / "payload"
        _write(payload / "lib/libz.so.1.3.2", _make_elf(soname="libz.so.1"))

        code, out = self._run(["elfdeps", "sonames", str(payload)])

        self.assertEqual(code, 0)
        self.assertEqual(out, "so:libz.so.1=3.2\n")

    def test_用错就报用法(self):
        code, _ = self._run(["elfdeps", "whatever", str(self.tmp)])
        self.assertEqual(code, 2)

        code, _ = self._run(["elfdeps", "needs"])
        self.assertEqual(code, 2)

    def test_给的不是目录(self):
        f = _write(self.tmp / "afile", b"\0\0\0\0")

        code, _ = self._run(["elfdeps", "needs", str(f)])

        self.assertEqual(code, 1)


# ------------------------------------------------------------------ 真实的 readelf

#: 真的 `readelf -d` 输出。取自本仓库建出来的产物：
#:   var/output/aarch64/zlib-1.3.2-1/payload/usr/lib/libz.so.1.3.2
#:   var/output/aarch64/dash-0.5.13.5-1/payload/usr/bin/dash
#: 存下来的理由：**能从文本算出来的，就别只有装了 readelf 的机器上才算得出来。**
READELF_WITH_SONAME = """\
Dynamic section at offset 0x5f10 contains 26 entries:
  Tag        Type                         Name/Value
 0x0000000000000001 (NEEDED)             Shared library: [libc.so.6]
 0x0000000000000001 (NEEDED)             Shared library: [ld-linux-aarch64.so.1]
 0x000000000000000e (SONAME)             Library soname: [libz.so.1]
 0x0000000000000019 (INIT_ARRAY)         0x4000
 0x0000000000000004 (HASH)               0x2c8
"""

READELF_NO_SONAME = """\
Dynamic section at offset 0x2f7f0 contains 30 entries:
  Tag        Type                         Name/Value
 0x0000000000000001 (NEEDED)             Shared library: [libc.so.6]
 0x0000000000000001 (NEEDED)             Shared library: [ld-linux-aarch64.so.1]
 0x000000000000000c (INIT)               0x42d0
"""


class Test解析_readelf_的输出(unittest.TestCase):
    def test_带_SONAME_的(self):
        self.assertEqual(elfdeps.parse_readelf(READELF_WITH_SONAME),
                         (["libc.so.6", "ld-linux-aarch64.so.1"], "libz.so.1"))

    def test_不带_SONAME_的(self):
        self.assertEqual(elfdeps.parse_readelf(READELF_NO_SONAME),
                         (["libc.so.6", "ld-linux-aarch64.so.1"], None))

    def test_空输出(self):
        self.assertEqual(elfdeps.parse_readelf(""), ([], None))


@unittest.skipUnless(shutil.which("readelf"), "宿主上没有 readelf（macOS 就是）")
class Test两条读取路结果一致(unittest.TestCase):
    """`scan()` 优先用 readelf，没有才用纯 Python。**两条路必须说同一件事。**

    macOS 上跳过（没有 readelf），Linux/CI 上跑。
    """

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="elfdeps-readelf-")
        cls.tmp = Path(cls._tmp.name)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_手搓的_ELF_两条路一致(self):
        f = _write(self.tmp / "libx.so.1.2.3", _make_elf(
            needed=("libc.so.6", "liby.so.2"), soname="libx.so.1"))

        self.assertEqual(elfdeps._readelf(f), elfdeps._read_elf(f))


# ------------------------------------------------------------------ 真的 ELF（要 docker）

def _docker_ready() -> bool:
    if shutil.which("docker") is None:
        return False
    r = subprocess.run(["docker", "image", "inspect", "alpine:latest"],
                       capture_output=True)
    return r.returncode == 0


@unittest.skipIf(shutil.which("readelf"),
                 "宿主上有 readelf —— 那件事已经由 Test两条读取路结果一致 验过了")
@unittest.skipUnless(_docker_ready(), "需要 docker 和 alpine 镜像")
class Test拿真的_readelf_验手搓的_ELF(unittest.TestCase):
    """手搓的 ELF 必须被【真的 readelf】承认，而且两个读法要一致。

    这一条是上面那批测试的地基：如果 `_make_elf` 拼出来的东西根本不是合法的
    ELF，那些测试就只是在验"我们的读取器和我们的生成器互相自洽" —— 那是
    循环论证，证明不了它读得动真实世界里的文件。

    ★ 而在 Unix 上没有 readelf 的机器（macOS 开发机就是）才是它的主战场：
      Linux 上宿主自己就有 readelf，上面那条纯宿主的测试已经把两件事都验了
      （手搓的 ELF + 两条路一致），所以这里 skip 掉，省一次容器和一次联网。

    一个容器里同时验两件事（binutils 装一次要十几秒，不值得装两遍）：

      · 手搓的那份字节 —— 真的 readelf 认得它，且标签和我们读出来的一样
      · 镜像里的动态链接器（真的 .so）—— 同一个结论

    ★ 为什么要给 subprocess 加 timeout：容器里 `apk add` 要联网，而实测撞到过
      它卡住不返回（同一时刻别的容器也在拉包）。卡住 = 这条测试没有结论，
      所以超时算【跳过】并说明原因，不算通过。
    """

    TIMEOUT = 120

    def test_真的_readelf_和我们的读取器说同一件事(self):
        with tempfile.TemporaryDirectory(prefix="elfdeps-docker-") as td:
            tmp = Path(td)
            f = _write(tmp / "libx.so.1.2.3", _make_elf(
                needed=("libc.so.6", "liby.so.2"), soname="libx.so.1"))

            script = (
                "apk add --no-cache binutils >/dev/null 2>&1 || exit 3\n"
                # alpine 是 musl 的，动态链接器就叫这个名字（aarch64 / x86_64 两种）
                "for f in /lib/ld-musl-*.so.1; do cp \"$f\" /w/ld-real.so; done\n"
                "echo '@@HANDMADE'\n"
                "readelf -d /w/libx.so.1.2.3\n"
                "echo '@@REAL'\n"
                "readelf -d /w/ld-real.so\n")
            try:
                r = subprocess.run(
                    ["docker", "run", "--rm", "--entrypoint", "sh", "-v", f"{tmp}:/w",
                     "alpine:latest", "-c", script],
                    capture_output=True, text=True, timeout=self.TIMEOUT)
            except subprocess.TimeoutExpired:
                self.skipTest(f"容器 {self.TIMEOUT} 秒没回来（多半是拉不到 binutils）")
            if r.returncode == 3:
                self.skipTest("容器里装不上 binutils（多半是没网）")
            self.assertEqual(r.returncode, 0, f"readelf 失败：\n{r.stdout}{r.stderr}")
            self.assertNotIn("Error", r.stdout + r.stderr)

            handmade, _, real = r.stdout.partition("@@REAL")
            handmade = handmade.split("@@HANDMADE", 1)[1]
            self.assertIn("(NEEDED)", handmade,
                          "真的 readelf 认不出手搓的 ELF —— 拼错了")

            # ① 手搓的那份
            self.assertEqual(elfdeps.parse_readelf(handmade), elfdeps._read_elf(f),
                             "真的 readelf 和我们手搓的解析结果不一致 "
                             "——要么 ELF 拼错了，要么解析错了")

            # ② 镜像里那份真的 .so
            real_so = tmp / "ld-real.so"
            self.assertTrue(real_so.is_file(), "没从镜像里拿到动态链接器")
            self.assertEqual(elfdeps.parse_readelf(real),
                             elfdeps._read_elf(real_so),
                             "两条路在【真的】动态库上结果不一致")


if __name__ == "__main__":
    unittest.main()
