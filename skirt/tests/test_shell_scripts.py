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

class TestPackKeepsUniqueDirs(unittest.TestCase):
    """每个包一个唯一的 pack 目录。

    ★ 这是踩出来的。原来所有包共用一个 $WORK/pack，里面的 inner.sh 和 epoch
      每个包重写一次，而挂载点始终是同一个宿主路径。在 macOS 上会读到【上一个
      包】的内容：

          /pack/inner.sh: line 13: syntax error: unterminated quoted string

      而那个文件在宿主上是正确的——它只是比上一个包的短，恰好断在第 13 行。

      症状很好认：失败的是【随机的包】（每跑一次换一批），而单独打某个包总是
      成功。因为单独跑时那个路径上没有"上一次"。
    """

    def test_packdir_is_per_package(self):
        pack = Path(__file__).resolve().parents[2] / "scripts" / "pack.sh"
        text = pack.read_text()

        self.assertIn('PACKDIR="$WORK/pack/$(basename "$d")"', text,
                      "pack.sh 里每个包的 pack 目录必须是唯一的")
        # 挂载和 epoch 读取都要用那个唯一目录，不能退回共用路径
        self.assertIn('-v "$PACKDIR:/pack:ro"', text)
        self.assertIn('"$PACKDIR/epoch"', text)
        self.assertNotIn('-v "$WORK/pack:/pack:ro"', text,
                         "不能挂共用的 pack 目录——那会读到上一个包的内容")

