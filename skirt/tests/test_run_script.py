"""生成的 run.sh 必须是合法的 shell。

【为什么守着这一条】

run.sh 是【拼】出来的：驱动的 header + build() + package() + steps.sh + 分阶段调度。
拼出来的东西容易在某一种组合下不是合法 shell —— 而那种错在 CI 里报的是

    /build/run.sh: line 4: syntax error near unexpected token `fi'

离"哪个组合"隔着一次构建。

★★ 实测撞上的那个：

    header 里为了不让"构建准备"（autoreconf / 补丁）在 package 阶段重跑一遍，
    给它包了一层 `if [ "${1:-all}" != "package" ]; then ... fi`。

    而 custom 类型的包（libcap / iproute2 / chrony …）没有 autoreconf、
    没有补丁 —— 那段是【空的】。而 `if …; then` 后面直接 `fi` 是非法的。

    我第一版验的时候拿的是 gzip（有补丁，那段非空），所以没撞上。
    **验的时候要挑边界那一头，不是顺手那一头。**

这条测试拿【每个配方】各生成一遍，然后交给 bash 自己 -n。
"""

import pathlib
import subprocess
import sys
import tomllib
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from skirtlib.build import make_build_script  # noqa: E402

RECIPES = pathlib.Path(__file__).resolve().parents[2] / "recipes"


class Test生成的脚本是合法shell(unittest.TestCase):
    def test_每个配方生成出来的都能过_bash_n(self):
        bad = []
        specs = sorted(RECIPES.glob("*/*/spec.toml"))
        self.assertTrue(specs, "一个配方都没找到——路径不对")
        for spec in specs:
            d = tomllib.loads(spec.read_text())
            for arch in ("x86_64", "aarch64"):
                try:
                    s = make_build_script(d, arch)
                except Exception as exc:                      # noqa: BLE001
                    bad.append(f"{d['package']['name']} [{arch}] 生成时就抛了 {exc!r}")
                    continue
                r = subprocess.run(["bash", "-n"], input=s, text=True,
                                   capture_output=True)
                if r.returncode != 0:
                    first = (r.stderr.strip().split("\n") or [""])[0]
                    bad.append(f"{d['package']['name']} [{arch}] bash -n 失败：{first}")
        self.assertFalse(
            bad,
            "生成的 run.sh 不是合法 shell。run.sh 是拼出来的，所以这只是"
            "某一种组合的问题——看下面哪个包：\n         " + "\n         ".join(bad))

    def test_构建准备那段只在_build_阶段跑(self):
        """★ 拆成两阶段之后，只该做一次的事不能做两次。

        实测：header 在文件顶部，每次调用都跑 —— 于是补丁被打了两遍，
        报的是「Reversed (or previously applied) patch detected」，
        看起来像"补丁过期"，其实那个补丁完全正确。
        """
        d = tomllib.loads((RECIPES / "tool/gzip/spec.toml").read_text())
        s = make_build_script(d, "x86_64")
        self.assertIn('if [ "${1:-all}" != "package" ]; then', s,
                      "gzip 有补丁，构建准备那段应该被阶段判断包住")
        # 而分阶段调度必须还在（它才是驱动真正调的那两个入口）
        self.assertIn('case "${1:-all}" in', s)
        #
        # ★★ 2026-10-17：这两条原来断言的是【整行字面文本】：
        #
        #       self.assertIn("  build)   build ;;", s)
        #       self.assertIn("  package) package ;;", s)
        #
        #   而那句在给 package 加一个收尾检查之后变成了
        #
        #       package) package; check_dest ;;
        #
        #   —— 于是这条测试红了，而它红的【不是它想守的东西】。它想守的是
        #   「package 阶段只跑 package、不跑 build」，那是【意图】；而整行
        #   文本是【实现细节的快照】。
        #
        #   ★ 守快照的测试会在每次无关改动时报假警，然后被人改成快照的新样子
        #     —— 于是它再也不守意图了。所以现在按【分支的语义】判：
        #     package 那一支里必须有 package、必须没有 build（反之亦然）。
        分支 = {}
        for line in s.splitlines():
            for 名 in ("all", "build", "package"):
                if line.startswith(f"  {名})"):
                    分支[名] = line[len(f"  {名})"):].strip()
        self.assertEqual(set(分支), {"all", "build", "package"},
                         "三个分支该都在")
        self.assertIn("package", 分支["package"], "package 阶段该调 package")
        self.assertNotIn("build", 分支["package"],
                         "package 阶段【不该】调 build —— 那会把构建准备做第二遍")
        self.assertIn("build", 分支["build"], "build 阶段该调 build")
        self.assertNotIn("package", 分支["build"],
                         "build 阶段不该调 package")


if __name__ == "__main__":
    unittest.main()
