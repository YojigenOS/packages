#!/usr/bin/env python3
"""shebang 写 sh 就必须真的能在 dash 下跑。

macOS 的 /bin/sh 是 bash，所以 `${VAR//x}` 这类写法在本地一直是好的；
Ubuntu 的 /bin/sh 是 dash，CI 上直接 "Bad substitution"。

2026-10-12：pack.sh（现在在 `skirtlib/pack.py`）在 CI 上就是这么炸的
（第 86 行 `${DIRS// /}`）。
本地跑一百遍都不会有反应。

这条检查用容器里的 dash 做语法检查——不执行，只看能不能解析。
"""

from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS = ROOT / "scripts"


def _dash_available() -> bool:
    if shutil.which("docker") is None:
        return False
    r = subprocess.run(["docker", "image", "inspect", "alpine:latest"],
                       capture_output=True)
    return r.returncode == 0


@unittest.skipUnless(_dash_available(), "需要 docker 和 alpine 镜像")
class TestDashCompatibility(unittest.TestCase):
    def test_写_sh_的脚本要能在_dash_下解析(self):
        # 只检查 shebang 是 sh 的。写 bash 的就该用 bash。
        sh_scripts = [p for p in sorted(SCRIPTS.glob("*.sh"))
                      if p.read_text().startswith("#!/bin/sh")]
        if not sh_scripts:
            self.skipTest("没有 shebang 是 sh 的脚本")

        names = " ".join(p.name for p in sh_scripts)
        r = subprocess.run(
            ["docker", "run", "--rm", "-v", f"{SCRIPTS}:/s:ro", "alpine:latest",
             "sh", "-c", f"for f in {names}; do dash -n /s/$f || echo \"FAIL $f\"; done"],
            capture_output=True, text=True)
        self.assertNotIn("FAIL", r.stdout + r.stderr,
                         f"这些脚本 shebang 是 sh 但 dash 解析不了：\n{r.stdout}{r.stderr}")
