#!/usr/bin/env python3
"""shell 脚本的静态检查。

【为什么需要一条「中文」的测试】这个项目的脚本里中文很多，而 shell 的变量
名不能和中文紧挨着：

    echo "测试（$ARCH）"      # ✗ shell 把「ARCH）」整体当变量名，报 unbound variable
    echo "测试（${ARCH}）"    # ✓ 花括号划清边界

2026-10-11 实测踩到：scripts/run-vm.sh 里

    echo "  启动 Yojigen OS（$ARCH）—— 退出 QEMU 按 Ctrl-A 然后 X"

那一行在 `exec qemu` 的正前方，于是脚本一直在那里静默退出——QEMU 从来没被
它启动过。而输出的最后一行是正常的「内核: …」，看起来像是跑完了。

同一类还有 5 处（scripts/smoke.sh 里 3 处 —— 那个脚本 2026-10-15 搬进了
`skirt smoke`，而 `skirt/tests/test_smoke.py` 里的夹具现在也归这条规矩管；
kernel/linux/steps.sh 注释里 1 处）。

**这不是「写得不好看」，是会真的炸。** 而它炸得很安静：脚本在最后一行之前
都是对的，退出码是 1 但没人看。
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "skirt"))


class TestShellVariableBoundaries(unittest.TestCase):
    #: $VAR 后面紧跟一个非 ASCII 字节
    BAD = re.compile(rb"\$([A-Za-z_][A-Za-z0-9_]*)(?=[\x80-\xff])")

    def _shell_files(self):
        for pat in ("scripts/*.sh", "recipes/*/*/steps.sh", "recipes/*/*/files/*"):
            for f in sorted(ROOT.glob(pat)):
                if f.is_file():
                    yield f

    def test_变量名和中文之间要有花括号(self):
        bad = []
        for f in self._shell_files():
            try:
                data = f.read_bytes()
            except OSError:
                continue
            for i, line in enumerate(data.splitlines(), 1):
                for m in self.BAD.finditer(line):
                    bad.append(f"{f.relative_to(ROOT)}:{i}  "
                               f"${m.group(1).decode()} 后面紧跟非 ASCII —— 写成 ${{{m.group(1).decode()}}}")
        self.assertFalse(
            bad,
            "shell 变量名和中文之间必须用花括号划清边界，否则 shell 会把中文"
            "的第一个字节当成变量名的一部分：\n         " + "\n         ".join(bad))
# ★ 这里原来有一个 `TestPackKeepsUniqueDirs`（"每个包一个唯一的 pack 目录"，
#   守着 macOS 上那个绑定挂载的坑）。2026-10-15 打包搬进 skirt 之后，那条判定
#   成了能直接调的 Python（`pack.pack_dir`），所以它的测试搬到了
#   `test_pack.py::Test每个包一个pack目录` —— 那边测的是行为，不是脚本里的字符串。
#   这个模块只留"shell 脚本的静态检查"这一件事。



class Test_标识符必须是_ASCII(unittest.TestCase):
    """shell 变量名和函数名只能是 `[A-Za-z_][A-Za-z0-9_]*`。

    ★★★ 2026-10-22 一天之内**【三次】**踩到同一件事，而三次的后果各不相同：

        参数=...      dash 当成命令 → "参数=: command not found"
        用法() {...}  dash 当场报 "Bad function name"（好，吵得响）
        $名字         语法能过，而【运行时常空】 —— 最糟的一种
        动作词=...    同上，于是判据那个 grep 用了空模式，永远不匹配

    最后一次是在 `service` 里：`动作词` 变空之后，判断"SysV 顺序还是我的顺序"
    那一段永远走错分支 —— 于是 `service 1panel-core status` 打印用法并 exit 1，
    而 1Panel 的安装器据此认为"服务启动失败"，**重试五次报错退出，而进程好好
    地在跑**。

    ★ 所以这一条不是风格问题：**它的后果全是"看起来成功了/失败了，而原因在
      别处"**。而它能被一条静态检查挡住。
    """

    # ★ 范围限定在 recipes/ 下：第一版写的是 `**/files/*`，于是它扫到了
    #   `.dsh/skills/**` 里的 .mjs（那些也有 shebang，而且满是 `q.hasAnchor=`）——
    #   一条检查本身在【不该管的地方】报错，比它漏报更让人不信任它。
    文件们 = [
        ("recipes/**/files/**/*", "配方带的可执行脚本"),
        ("recipes/**/steps.sh", "构建步骤"),
        ("recipes/**/scripts/*", "apk 的安装脚本"),
    ]

    def test_没有非_ASCII_的变量名和函数名(self):
        import pathlib
        import re
        root = pathlib.Path(__file__).resolve().parents[2]
        坏 = []
        for 模式, _说明 in self.文件们:
            for f in root.glob(模式):
                if not f.is_file():
                    continue
                try:
                    text = f.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue
                if not text.startswith("#!"):
                    continue          # 只查脚本（有 shebang 的）
                for i, line in enumerate(text.splitlines(), 1):
                    代码 = line.split("#", 1)[0]
                    # 赋值：行首的 名字=
                    for m in re.finditer(r'(?:^|[;&|]\s*|\bthen\s+|\bdo\s+)([^\s=()"\']+)=', 代码):
                        名 = m.group(1)
                        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', 名):
                            坏.append(f"{f.relative_to(root)}:{i}  变量名 {名!r}")
                    # 函数定义：名字()
                    for m in re.finditer(r'^\s*([^\s()]+)\(\)', 代码):
                        名 = m.group(1)
                        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', 名):
                            坏.append(f"{f.relative_to(root)}:{i}  函数名 {名!r}")
        self.assertFalse(坏, "shell 里用了非 ASCII 的标识符（POSIX 只允许 ASCII）：\n  "
                             + "\n  ".join(坏[:20]))
