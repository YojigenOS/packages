"""从一个包的 payload 里读出它【真正】依赖哪些动态库，以及它提供哪些。

    在 Python 里用：   from skirtlib import elfdeps
                       needed, provided = elfdeps.scan(payload)
    命令行上用它：     cd skirt && python3 -m skirtlib.elfdeps needs   <payload 目录>
                       cd skirt && python3 -m skirtlib.elfdeps sonames <payload 目录>

    needs    → 一行一个 so:<soname>（读 ELF 的 DT_NEEDED）
    sonames  → 一行一个 so:<soname>=<版本后缀>（读 .so 文件自己的 DT_SONAME）

── 为什么需要它 ────────────────────────────────────────────────────────────

apk 支持用 soname 表达依赖，两端都能自动：

    提供方   provides: so:libz.so.1=1.3.2
    使用方   depends:  so:libc.so.6

而 **mkpkg 不扫描 ELF**——实测它建出来的包 provides 和 depends 都是空的。
Alpine 的 so: 是 abuild + scanelf 生成的，不是 apk 自己的。

所以在我们这儿，依赖一直靠人在 spec.toml 里手写，而手写就会漏。已经漏过的：

    gawk     NEED libmpfr.so.6 + libgmp.so.10，只声明了 glibc
    apk-tools NEED libssl.so.3（通过 libapk），得靠人想起来写 openssl

而文档里那份《未声明依赖的审计》是【离线的一次性分析】——发现了就手改进
配方，下次可能又漂。这个模块把同一件事接进打包流程：包建出来的时候就知道
自己依赖什么。

── 为什么它在这里，而不是 scripts/ ──────────────────────────────────────

它原来是 `scripts/elf-deps.py`，由 `scripts/pack.sh` 里那段内嵌 Python **按
文件路径**硬加载（`importlib.util.spec_from_file_location`）。而那条路的后果是
**测试 import 不进来**：文件不在任何包的路径上，只有"知道它躺在哪个文件里"
的调用方拿得到它。

所以它搬进 `skirtlib/`，pack.sh 那边改成正常 import。搬家没有改任何判定逻辑
——`scan()` 的输入输出、两个读取路径的选择、provides 的版本后缀算法，都和
搬之前逐行一致；`skirt/tests/test_elfdeps.py` 守着这些。

── 为什么不用 readelf 就完了 ──────────────────────────────────────────────

因为打包脚本跑在【宿主】上，而宿主是 macOS，没有 readelf。Linux（CI）上有。
所以优先用 readelf，没有就走纯 Python——ELF 的动态段结构不复杂，读
DT_NEEDED / DT_SONAME 两个 tag 就够。

两条路的结果必须一致：`skirt/tests/test_elfdeps.py` 里有一条把它们对齐的测试
（主机上没有 readelf 时它会 skip，所以本机跑不出来，CI 上跑得出来）。
"""

from __future__ import annotations

import shutil
import struct
import subprocess
import sys
from pathlib import Path

# ELF 里我们关心的两个动态段 tag
DT_NULL = 0
DT_NEEDED = 1
DT_SONAME = 14

# 只处理这两种机器（我们只有这两个架构），它们的 ELF 头布局一致
EM_386 = 3
EM_X86_64 = 62
EM_AARCH64 = 183
SUPPORTED_MACHINES = {EM_386, EM_X86_64, EM_AARCH64}


def _is_elf(path: Path) -> bool:
    """读魔数。不靠扩展名——.so.1.0 之类没有规律。"""
    try:
        with path.open("rb") as fh:
            return fh.read(4) == b"\x7fELF"
    except OSError:
        return False


def _read_elf(path: Path) -> tuple[list[str], str | None]:
    """纯 Python 读一个 ELF 的 (NEEDED 列表, SONAME)。

    只做够用的事：定位节头里的 .dynamic，读出那两个 tag 指向的字符串。
    不做重定位、不看不认识的段、失败就返回空——这不是一个通用 ELF 工具。
    """
    with path.open("rb") as fh:
        data = fh.read()

    if data[:4] != b"\x7fELF":
        return [], None

    ei_class = data[4]          # 1 = 32 位，2 = 64 位
    ei_data = data[5]           # 1 = 小端，2 = 大端

    if ei_class == 1:
        end = "<" if ei_data == 1 else ">"
        # e_shoff(5) e_shentsize(11) e_shnum(12) e_shstrndx(13)
        e_shoff, = struct.unpack_from(end + "I", data, 0x20)
        e_shentsize, e_shnum = struct.unpack_from(end + "HH", data, 0x2E)
        sh_fmt, sh_size = end + "IIIIIIIIII", 40
    elif ei_class == 2:
        end = "<" if ei_data == 1 else ">"
        e_shoff, = struct.unpack_from(end + "Q", data, 0x28)
        e_shentsize, e_shnum = struct.unpack_from(end + "HH", data, 0x3A)
        sh_fmt, sh_size = end + "IIQQQQIIQQ", 64
    else:
        return [], None

    if not e_shoff or not e_shnum:
        return [], None

    sections = []
    for i in range(e_shnum):
        off = e_shoff + i * e_shentsize
        if off + sh_size > len(data):
            break
        sections.append(struct.unpack_from(sh_fmt, data, off))

    def section_data(sec) -> bytes:
        # 32 位: sh_offset 在第 5 个字段；64 位: 也在第 5 个
        sh_offset = sec[4]
        sh_size = sec[5]
        return data[sh_offset : sh_offset + sh_size]

    # .dynamic 的节类型是 SHT_DYNAMIC(6)
    dyn_secs = [s for s in sections if s[1] == 6]
    if not dyn_secs:
        return [], None

    # 字符串表：.dynamic 里的 d_val 是【表内偏移】，要找到对应的字符串表。
    # 按 sh_link 指过去（这是规范做法）。
    strtab_index = dyn_secs[0][6]
    if strtab_index >= len(sections):
        return [], None
    strtab = section_data(sections[strtab_index])

    # 节头字符串表用来找 .dynstr——如果 sh_link 不靠谱，退回到按名字找
    #
    # ★ 这条路实际走不到（2026-10-15 搬进 skirtlib 时看出来的）：下面读的是
    #   `data[name_off:...]`，而 sh_name 是【节头字符串表内】的偏移，不是文件
    #   里的偏移。所以除非那个字符串表恰好在文件偏移 0 上，这段找不出名字。
    #   真正生效的永远是上面 sh_link 那条。
    #
    #   **没有改它**：搬家只搬不改。它是一条"看起来在用、其实不会命中"的兜底，
    #   留着不会把对的判成错的（strtab 已经是空的了，命中失败也只是继续空）。
    if not strtab:
        for sec in sections:
            if sec[0] == 0:      # SHT_NULL 没有名字
                continue
            name_off = sec[0]
            end = data.index(b"\0", name_off)
            if data[name_off:end] == b".dynstr":
                strtab = section_data(sec)
                break

    def cstr(blob: bytes, off: int) -> str:
        if off >= len(blob):
            return ""
        stop = blob.find(b"\0", off)
        return blob[off : stop if stop >= 0 else len(blob)].decode("utf-8", "replace")

    # 64 位下 d_tag 是 8 字节、d_val 也是 8 字节；32 位都是 4 字节
    d_fmt = end + ("qQ" if ei_class == 2 else "iI")
    d_size = 16 if ei_class == 2 else 8

    needed: list[str] = []
    soname: str | None = None
    for sec in dyn_secs:
        blob = section_data(sec)
        for off in range(0, len(blob) - d_size + 1, d_size):
            tag, val = struct.unpack_from(d_fmt, blob, off)
            if tag == DT_NULL:
                break
            if tag == DT_NEEDED:
                name = cstr(strtab, val)
                if name:
                    needed.append(name)
            elif tag == DT_SONAME:
                soname = cstr(strtab, val)
        break       # 只看第一个 .dynamic

    return needed, soname


def parse_readelf(text: str) -> tuple[list[str], str | None]:
    """`readelf -d` 的输出 → (NEEDED 列表, SONAME)。

    从 `_readelf()` 里拆出来的一小块纯函数：**能从文本算出来的，就不要
    只有在有 readelf 的机器上才算得出来**。所以那份输出可以存下来当测试的
    输入（见 skirt/tests/test_elfdeps.py），不需要真跑一遍 readelf。
    """
    needed: list[str] = []
    soname: str | None = None
    for line in text.splitlines():
        if "(NEEDED)" in line and "[" in line:
            needed.append(line[line.index("[") + 1 : line.rindex("]")])
        elif "(SONAME)" in line and "[" in line:
            soname = line[line.index("[") + 1 : line.rindex("]")]
    return needed, soname


def _readelf(path: Path) -> tuple[list[str], str | None] | None:
    """用 readelf 读。没有 readelf 就返回 None（调用方走纯 Python）。"""
    exe = shutil.which("readelf")
    if not exe:
        return None

    try:
        proc = subprocess.run([exe, "-d", str(path)], capture_output=True, text=True)
    except OSError:
        return None
    if proc.returncode != 0:
        return None

    return parse_readelf(proc.stdout)


def scan(payload: Path) -> tuple[list[str], dict[str, str]]:
    """扫一个 payload。

    返回 (依赖的 soname 列表, {提供的 soname: 版本后缀})。

    ★ 版本后缀：libz.so.1.3.2 的 SONAME 是 libz.so.1，而 apk 的 provides 写作
      `so:libz.so.1=1.3.2`——等于号后面是【完整文件名去掉 so: 名字】的那部分。
      这个格式是从 Alpine 的产物里看来的（zlib 的 provides 就是
      `so:libz.so.1=1.3.2`）。apk 靠它做版本比较。
    """
    needed: set[str] = set()
    provided: dict[str, str] = {}

    for path in sorted(payload.rglob("*")):
        # 符号链接不算：libz.so.1 → libz.so.1.3.2 两个都扫的话会重复。
        # 真文件才是权威（SONAME 写在它里面）。
        if path.is_symlink() or not path.is_file():
            continue
        if not _is_elf(path):
            continue

        got = _readelf(path)
        if got is None:
            got = _read_elf(path)
        n, s = got

        needed.update(n)
        if s:
            # ★ 只收 .so 文件，不收可执行文件。可执行文件也可以有 SONAME，
            #   但那个名字不该被别的包当库来依赖。
            if ".so" in path.name:
                full = path.name
                suffix = full[len(s):].lstrip(".") if full.startswith(s) else ""
                provided[s] = suffix

    return sorted(needed), provided


def main(argv: list[str]) -> int:
    if len(argv) != 3 or argv[1] not in ("needs", "sonames"):
        print(__doc__, file=sys.stderr)
        return 2

    mode, payload = argv[1], Path(argv[2])
    if not payload.is_dir():
        print(f"不是目录: {payload}", file=sys.stderr)
        return 1

    needed, provided = scan(payload)
    if mode == "needs":
        for n in needed:
            print(f"so:{n}")
    else:
        for name in sorted(provided):
            suffix = provided[name]
            print(f"so:{name}={suffix}" if suffix else f"so:{name}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
