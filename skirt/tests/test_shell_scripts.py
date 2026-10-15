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

同一类还有 5 处（scripts/smoke.sh 里 3 处、kernel/linux/steps.sh 注释里 1 处）。

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

