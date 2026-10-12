#!/usr/bin/env python3
"""构建顺序的测试。

只有一个主题：**顺序必须是输入的纯函数**。

`skirt order` 存在的意义是「让重建顺序可复现」——在它之前，重建顺序只存在于
某个人执行过的那条命令里。所以一个每次都不一样的"可复现顺序"是自相矛盾的，
而这件事一度真的发生了：`graphlib.TopologicalSorter` 内部用 set 存前驱和后继，
迭代顺序取决于 Python 的字符串 hash 随机化，每个进程都不一样。实测同一份输入
连跑三次给出三种顺序。

★ 这类 bug 的可怕之处是它**只在对比两次运行的时候才看得见**。单跑一次永远
  "通过"。所以这里的测试必须真的起两个不同 hash seed 的进程去比。
"""

from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # packages/skirt
PKGS = ROOT.parent                                     # packages

sys.path.insert(0, str(ROOT))

from skirtlib.deps import load_host_provides           # noqa: E402
from skirtlib.order import build_graph, build_order    # noqa: E402


def _order_in_subprocess(seed: str) -> str:
    """在指定的 PYTHONHASHSEED 下算一次顺序，返回一行。"""
    env = dict(os.environ, PYTHONHASHSEED=seed)
    r = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, 'skirt');"
         "from skirtlib.order import build_order;"
         "print(' '.join(build_order('aarch64')[0]))"],
        cwd=PKGS, env=env, capture_output=True, text=True, timeout=180,
    )
    if r.returncode != 0:
        raise AssertionError(f"子进程失败（seed={seed}）：\n{r.stderr[-2000:]}")
    return r.stdout.strip()


class TestOrderIsDeterministic(unittest.TestCase):

    def test_same_hash_seed_twice_is_identical(self):
        """对照组的基线：同一个 seed 跑两次必须一样。

        ★ 这一条先跑，是因为它是判据的一部分。如果它都不过，说明不确定性另有
          来源（文件系统顺序之类），那下面的测试会误导人。
        """
        self.assertEqual(_order_in_subprocess("0"), _order_in_subprocess("0"))

    def test_different_hash_seeds_agree(self):
        """★★ 这条是真正守规矩的那个。

        曾经坏掉的表现：seed 0 / 1 / 2 给出三种顺序。
        """
        orders = {s: _order_in_subprocess(s) for s in ("0", "1", "2")}
        distinct = set(orders.values())
        self.assertEqual(
            len(distinct), 1,
            "构建顺序随 PYTHONHASHSEED 变化——有 set 的迭代顺序漏进了结果。\n"
            + "\n".join(f"  seed={s}: {o.split()[:6]}…" for s, o in orders.items()),
        )

    def test_order_is_deterministic_within_one_process(self):
        """同一个进程里连着算两次也要一样（防止缓存/dict 重排之类）。"""
        self.assertEqual(build_order("aarch64")[0], build_order("aarch64")[0])


class TestOrderIsValid(unittest.TestCase):
    """顺序对不对，不看它长什么样，看它满不满足那件事：依赖排在前面。"""

    def test_every_edge_points_forward(self):
        order, _ = build_order("aarch64")
        graph, _ = build_graph("aarch64", load_host_provides(), order)
        pos = {n: i for i, n in enumerate(order)}

        bad = [(n, p) for n, preds in graph.items() for p in preds
               if pos[p] >= pos[n]]
        self.assertEqual(
            bad, [],
            "这些依赖排在需求方后面：\n"
            + "\n".join(f"  {n} 需要 {p}" for n, p in bad[:10]),
        )

    def test_every_recipe_appears_exactly_once(self):
        order, _ = build_order("aarch64")
        self.assertEqual(len(order), len(set(order)),
                         "顺序里有重复的包——拓扑排序不该产出重复")


if __name__ == "__main__":
    unittest.main()
