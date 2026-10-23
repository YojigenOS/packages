"""`pack.py` 里那张 SYSTEM_GROUPS 和 filesystem 的 /etc/group 不许漂移。

★★ 为什么需要这条（2026-10-23）：

   同一个事实写在了两个地方：

     · `recipes/data/filesystem/files/etc/group`    —— 装出来的系统用这份
     · `skirtlib/pack.py` 的 `SYSTEM_GROUPS`        —— 打包容器用它认组名

   两处必须一致。而**不一致的症状特别远**：

       构建日志说"组属主清单（18 条）"、"从清单读到 18 个"，
       而 .apk 里是 `group: nobody`

   —— 因为 `mkpkg` 记的是组【名字】，在容器的 `/etc/group` 里查不到 GID
   就退回 `nobody`（65534）。

★★ 判据：**一个事实写两遍，就要有一处测试盯着它们相等。**
   否则改一处的时候另一处静默留在旧值，而报错的地方离成因隔了好几层。

★ 用【读源码】而不是 `import`：这个目录里的测试跑法是
  `python3 -m unittest discover -s skirt/tests`，`skirtlib` 不在 `sys.path` 上。
  `test_collect.py` 也是这么做的（它读 `collect.py` 的文本）。
"""

from __future__ import annotations

import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
PACK_PY = ROOT / "skirt" / "skirtlib" / "pack.py"
GROUP_FILE = ROOT / "recipes" / "data" / "filesystem" / "files" / "etc" / "group"


def _pack_groups() -> dict[int, str]:
    """从 pack.py 的源码里抠出 SYSTEM_GROUPS 那张表。"""
    src = PACK_PY.read_text(encoding="utf-8")
    m = re.search(r"SYSTEM_GROUPS[^=]*=\s*\((.*?)\)\s*\n", src, re.S)
    if not m:
        raise AssertionError("pack.py 里找不到 SYSTEM_GROUPS 的定义")
    out: dict[int, str] = {}
    for gid, name in re.findall(r"\(\s*(\d+)\s*,\s*\"([a-z0-9_-]+)\"\s*\)", m.group(1)):
        out[int(gid)] = name
    if not out:
        raise AssertionError("SYSTEM_GROUPS 抠出来是空的 —— 正则和实际写法对不上了")
    return out


def _recipe_groups() -> dict[str, int]:
    """把 filesystem 的 /etc/group 读成 {名字: GID}。"""
    out: dict[str, int] = {}
    for line in GROUP_FILE.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split(":")
        if len(parts) >= 3 and parts[2].isdigit():
            out[parts[0]] = int(parts[2])
    return out


class Test_PACK_SYSTEM_GROUPS_和_etc_group_一致(unittest.TestCase):
    def test_两个文件都在(self):
        self.assertTrue(PACK_PY.is_file(), f"找不到 {PACK_PY}")
        self.assertTrue(GROUP_FILE.is_file(), f"找不到 {GROUP_FILE}")

    def test_每一个_SYSTEM_GROUPS_都在配方里_且_GID_相同(self):
        配方 = _recipe_groups()
        坏 = []
        for gid, name in sorted(_pack_groups().items()):
            if name not in 配方:
                坏.append(f"{name}: pack.py 里有（GID {gid}），/etc/group 里没有")
            elif 配方[name] != gid:
                坏.append(f"{name}: pack.py 说 {gid}，/etc/group 说 {配方[name]}")
        self.assertFalse(坏, "pack.py 的 SYSTEM_GROUPS 和 /etc/group 不一致：\n  "
                         + "\n  ".join(坏))

    def test_utmp_是_43(self):
        """这条单列出来 —— 它是 2026-10-23 那一整轮排查的起因。"""
        self.assertEqual(_pack_groups().get(43), "utmp")


if __name__ == "__main__":
    unittest.main()


class Test_inner_sh_语法正确(unittest.TestCase):
    """`inner.sh` 生成出来必须是【能跑的】shell。

    ★★ 为什么要有这条（2026-10-23）：

       我在里面加了个 for 循环补组名，而 Python 的隐式字符串拼接把
       `... >> /etc/group` 和 `done` 拼成了一行：

           ... >> /etc/groupdone

       于是整个 `skirt pack` 报：

           /pack/inner.sh: line 48: syntax error: unexpected end of file
           (expecting "done")

       —— **一个 Python 字符串拼接的问题，症状是一条 shell 语法错误。**

    ★ 判据：**生成的脚本要用 `sh -n` 验一遍。** 这比"读一遍看着对"可靠得多
      （我读了两遍都觉得对）。
    """

    def test_生成出来的_inner_sh_过得了_sh_n(self):
        import subprocess
        import sys
        sys.path.insert(0, str(ROOT / "skirt"))
        from skirtlib.pack import inner_script, Job       # noqa: PLC0415

        job = Job(product=ROOT, meta={}, name="probe", version="1.0-r0",
                  apk="probe.apk", epoch="0")
        src = inner_script(job, [], [], signed=False)
        # 用 `sh -n` 只做语法检查，不执行
        r = subprocess.run(["sh", "-n"], input=src, text=True,
                           capture_output=True, timeout=30)
        self.assertEqual(r.returncode, 0,
                         f"inner.sh 语法错误：\n{r.stderr}")
