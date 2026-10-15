#!/usr/bin/env python3
"""shebang 写 sh 就必须真的能在 dash 下跑。

macOS 的 /bin/sh 是 bash，所以 `${VAR//x}` 这类写法在本地一直是好的；
Ubuntu 的 /bin/sh 是 dash，CI 上直接 "Bad substitution"。

2026-10-12：pack.sh（现在在 `skirtlib/pack.py`）在 CI 上就是这么炸的
（第 86 行 `${DIRS// /}`）。本地跑一百遍都不会有反应。

这条检查用容器里的 dash 做语法检查——不执行，只看能不能解析。

★ 而它查【谁】这件事改过一次（2026-10-15）。

原来它 glob 的是 `scripts/*.sh`。而那天 `scripts/` 里三个主流程脚本都收进了
`skirt`（只剩 `keys.sh`，而它是 `#!/bin/bash`）——于是这个测试**每次都 skip**，
而 skip 看起来和"过了"一样。**真正的检查对象从来不是那些**：

    recipes/<类>/<名字>/files/*      进包、在【目标机】上跑
    recipes/<类>/<名字>/scripts/*     apk 的安装/升级脚本，也在目标机上跑

而那 8 个的目标 shell 正是 dash（`/bin/sh → dash-binsh`），而且是**真机开机时
跑的**——`init`、`05-hostname`、`10-grow-root`、`15-network`、`20-sshd`、
`ensure-authorized-keys`、`post-install`、`post-upgrade`。它们比 `pack.sh`
更该被查：`pack.sh` 炸了是构建失败（响的），这些炸了是**机器起不来**。

★ 而 `steps.sh` 不在这里查：驱动用 bash 跑它们，所以它们的目标 shell 不是 dash。
  （`lsof/steps.sh` 里有个 `#!/bin/sh`，那在 heredoc 里，不是 shebang。）
"""

from __future__ import annotations

import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent


#: ★ 下面这十条是【dash -n 看不见的】。
#:
#:   2026-10-15 实测：`dash -n`（只解析、不执行）对常见 bashism 的反应是——
#:
#:       a=(1 2)         拒绝 ✓        ${a[0]}        接受 ✗
#:       [[ 1 == 1 ]]    接受 ✗        ${x:1:2}       接受 ✗
#:       ${x^^}          接受 ✗        function f {   接受 ✗
#:       let i=1         接受 ✗        ${X//b/B}      接受 ✗
#:
#:   而 `${X//b/B}` **正是这个文件当初要抓的那一个**（`${DIRS// }` 那次）。
#:   它在 dash 里是【运行时】才报 "Bad substitution" —— 语法是合法的，所以
#:   `-n` 永远看不见它。
#:
#:   ★ 所以这条检查从写下的那天起就做不成它要做的事。今天补上这份静态名单
#:     （名单和 `distro/tests/test_sh_scripts.py` 那份是同一套）。
#:
#:   而 `-n` 那条留着 —— 它抓的是另一类（真的解析不了的东西），那一类它有效。
BASHISMS = [
    (re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*:\s*[0-9]"), "${VAR:0:12}", "子串扩展，POSIX 里没有"),
    (re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*/\S"), "${VAR/a/b}", "替换扩展，POSIX 里没有"),
    (re.compile(r"(?<!\$)\(\([^)]*\)\)"), "(( ))", "算术命令（$(( )) 才是 POSIX）"),
    (re.compile(r"^\s*[A-Za-z_][A-Za-z0-9_]*=\("), "name=(...)", "数组赋值"),
    (re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*\[@\]\}"), "${a[@]}", "数组展开"),
    (re.compile(r"^\s*\[\["), "[[ ]]", "bash 的条件表达式"),
    (re.compile(r"\bfunction\s+\w+\s*\("), "function", "bash 的函数写法"),
    (re.compile(r"\bsource\s+\S"), "source", "POSIX 是 ."),
    (re.compile(r"\becho\s+-e\b"), "echo -e", "POSIX 的 echo 没有 -e"),
    (re.compile(r"\$'"), "$'...'", "ANSI-C 引用"),
]


def _targets() -> list[Path]:
    """会进包、在目标机上跑的那些脚本（相对仓库根）。

    两类：配方的 `files/`（铺进包的文件）和 `scripts/`（apk 的安装/升级脚本）。
    只看 shebang 是 `#!/bin/sh` 的 —— 写 bash 的就该用 bash。
    """
    out = []
    for pat in ("recipes/*/*/files/*", "recipes/*/*/scripts/*"):
        for p in sorted(ROOT.glob(pat)):
            if p.is_file() and p.read_text(errors="replace").startswith("#!/bin/sh"):
                out.append(p.relative_to(ROOT))
    return out


def _dash_available() -> bool:
    if shutil.which("docker") is None:
        return False
    r = subprocess.run(["docker", "image", "inspect", "alpine:latest"],
                       capture_output=True)
    return r.returncode == 0


@unittest.skipUnless(_dash_available(), "需要 docker 和 alpine 镜像")
class TestDashCompatibility(unittest.TestCase):
    def test_那八个脚本里没有_dash_看不见的_bashism(self):
        """★ 这一条才是真的检查（见 BASHISMS 上面那段）。

        只扫【行】—— 注释里出现这些字样不算（用 `#` 剥掉再扫）。
        """
        targets = _targets()
        self.assertTrue(targets, "一个 shebang 是 sh 的脚本都没找到")

        bad = []
        for rel in targets:
            for n, line in enumerate((ROOT / rel).read_text(errors="replace").split("\n"), 1):
                code = line.split("#", 1)[0] if not line.lstrip().startswith("#") else ""
                for pat, name, why in BASHISMS:
                    if pat.search(code):
                        bad.append(f"{rel}:{n}  {name}  —— {why}\n      {line.strip()[:90]}")
        self.assertFalse(bad, "这些脚本 shebang 是 sh，但用了 dash 不认的写法：\n  "
                              + "\n  ".join(bad))

    def test_每个_sh_脚本都要能在_dash_下解析(self):
        targets = _targets()
        # ★ 找不到了要【响】。原来那句 skipTest 害了这件事一整天：
        #   检查对象没了的时候，它安静地绿着。
        self.assertTrue(targets, "一个 shebang 是 sh 的脚本都没找到 —— "
                                 "检查对象是不是搬走了？去看 _targets() 的 glob")

        # ★ 必须装【真的 dash】，不能用 alpine 自带的 /bin/sh。
        #
        #   Alpine 的 /bin/sh 是 busybox ash，而它【比 dash 宽】：
        #
        #       x=abcdef; echo ${x//c/C}     ash 接受 ✗   dash 拒绝 ✓
        #       [[ 1 == 1 ]] && echo y       ash 接受 ✗   dash 拒绝 ✓
        #
        #   而 `${VAR//x}` 正是 2026-10-12 那次事故的形状。用 sh 代替 = 检查不到。
        #   实测这个安装 2 秒（`apk add --no-cache dash`）。
        #
        # 整仓挂进去 —— 目标分散在十几个配方目录里，一个挂载点搞不定。
        listing = "\n".join(str(t) for t in targets)
        script = (
            "apk add --no-cache dash >/dev/null 2>&1 || exit 9\n"
            "fail=0\n"
            "while IFS= read -r f; do\n"
            "  dash -n \"/repo/$f\" || { echo \"FAIL $f\"; fail=1; }\n"
            "done <<'EOF'\n" + listing + "\nEOF\n"
            "exit $fail\n"
        )
        r = subprocess.run(
            ["docker", "run", "--rm", "-v", f"{ROOT}:/repo:ro", "alpine:latest",
             "sh", "-c", script],
            capture_output=True, text=True)
        if r.returncode == 9:
            self.skipTest("容器里装不上 dash（网络）—— 这一条没验")
        self.assertEqual(r.returncode, 0,
                         f"这些脚本 shebang 是 sh 但 dash 解析不了：\n{r.stdout}{r.stderr}")
        self.assertNotIn("FAIL", r.stdout,
                         f"这些脚本 shebang 是 sh 但 dash 解析不了：\n{r.stdout}{r.stderr}")
