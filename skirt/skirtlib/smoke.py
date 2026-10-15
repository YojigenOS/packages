"""skirt 的冒烟测试 —— 跑一次真的构建，验证整条链是通的。

    skirt smoke                  # aarch64（默认，宿主架构）
    skirt smoke --arch x86_64
    skirt smoke --kernel-tar /路径/linux-6.x.tar.xz

大约几分钟（要 docker：真的下载、真的解压、真的编译、真的运行）。它不替代 CI
的全量自举，而是回答一个更小的问题：

    【现在这条链，此刻，是通的吗？】

── 为什么不放进 skirt/tests/ ────────────────────────────────────────────

因为那是两件不同的事，而混在一起会让两件都坏掉：

    python3 -m unittest discover -s skirt/tests      # <1 秒，纯逻辑，不需要 docker
    ./skirt/skirt smoke                              # 几分钟，要 docker，要真的建包

★ 而按 AGENTS.md 那条硬规矩，构建都在 CI 上跑、本地不构建任何东西 —— 所以
  「跑单元测试」这条路由 CI 的 push 触发，「跑冒烟」是一条**显式的**动作
  （它要几分钟，还要网络）。两者的调用方式、代价、失败含义都不一样。

★ 而这一条的**判定部分**（尤其第⑥项的判据）在 Python 里，由
  `skirt/tests/test_smoke.py` 守着 —— 那是能测的那一半。留在 shell 里的话，
  "一小时之内就算抄了墙钟"这条判断只能靠读一遍才发现它写错了。

── 为什么要它 ────────────────────────────────────────────────────────────

2026-10-11 那天修了三个「静默产出错误结果」的驱动 bug：

    run.sh 没有 set -e     → 构建失败变成「成功」，产出一个残缺的包
    源码在宿主解压         → APFS 大小写不敏感，内核源码 13 组文件只剩 13 个
    产物目录模式丢了       → /tmp 变成 0755，自举卡在最后一个包 glibc

三个都不是「跑不通」，而是「跑通了但结果是错的」。而 81 个单元测试
一个都没抓到——它们全都不碰 Docker，而三个 bug 都在 Docker 那一侧。

所以这件事做的不是单元测试，是【端到端】：真的下载、真的解压、真的编译、
真的运行、真的看输出对不对。

── 每一项检查对应什么 ────────────────────────────────────────────────────

  ① 解压完整性   对应「源码在宿主解压」那个 bug。不编译，只要几秒。
  ② 端到端构建   对应「run.sh 没有 set -e」。构建失败必须让这里失败。
  ③ 产物形态     对应「产出了一个空包也算成功」。
  ④ 它能跑       对应「产物在容器里存在，但在真实根文件系统里跑不起来」。
  ⑤ 中文能显示   对应「中文优先」这个承诺——它实现得好好的，但从没被验证过。
  ⑥ 可复现归一化 对应「.apk 的可复现只挂在 SOURCE_DATE_EPOCH 这一个开关上，
                 而链上没有第二处归一化，也没有检查守着」。见客观性.md。

⑤ 用的是 hello 自己的设计意图。它的配方注释里写着：

    「跑 LANG=zh_CN.UTF-8 hello 看到中文，就说明 gettext、NLS、locale
      三件事都对了——这三件事各自都可能静默失败。」

那句话写在 2026-10-10，而直到 2026-10-11 才有人真的跑了一次。

── 它是从 shell 搬过来的（2026-10-15）────────────────────────────────────

原来是 `scripts/smoke.sh`（225 行）。搬过来时判定进了函数（
`judge_probe` / `judge_mtime` / `payload_shape`），容器调用只剩三个 argv
生成器 —— 于是"六项检查各自的判据"第一次有了测试。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .common import OUTPUT_DIR, BUILD_DIR, PKGS_DIR, ROOT, die, rmtree_force

#: 探针包。它小、它有自己的中文设计意图（见模块顶上 ⑤）。
PROBE = "hello"

#: 冒烟默认用哪个构建环境。和 shell 版一致：`SKIRT_BUILDENV` 优先，
#: 否则用我们自己那个（每个包都是我们建的）。
DEFAULT_BUILDENV = "yojigenos/devel"

#: 内核源码里【应该】有多少组只差大小写的路径（xt_MARK.h / xt_mark.h 之类）。
#: 在宿主上解压就会互相覆盖 —— 实测 13 组全变成 0 组。
CASE_COLLISIONS = 13

#: ⑥ 那条：mtime 落在这么久之内，就说明它抄的是墙钟，不是 SOURCE_DATE_EPOCH。
FRESH_WINDOW = 3600

#: 容器里读包的镜像：要一个【带 apk-tools 的】环境。不用 BUILDENV —— devel 里
#: 没有 apk（实测 `command -v apk` 空），那正是"盘里没有 apk"记过的那个坑的同族。
APK_IMAGE = "alpine:latest"


# ------------------------------------------------------------------ 判定（可测）

def parse_collisions(text: str) -> int:
    """容器回报的那个数字。读不出来返回 0（调用方按"不对"处理）。"""
    line = [l for l in text.splitlines() if l.strip()]
    if not line:
        return 0
    try:
        return int(line[-1].strip())
    except ValueError:
        return 0


def payload_shape(payload: Path) -> tuple[int, bool]:
    """产物形态： (文件+符号链接的条数, 那个可执行文件在不在)。

    「收集成功」不等于「收到了东西」—— 空产物曾经也报成功。
    """
    count = sum(1 for p in payload.rglob("*") if p.is_file() or p.is_symlink())
    binary = (payload / "usr/bin" / PROBE)
    return count, (binary.is_file() and os.access(binary, os.X_OK))


def parse_probe_output(text: str) -> dict[str, str]:
    """把容器里那三行 `EN=… / ZH=… / TW=…` 读回来。

    ★ 程序自己的输出里也可能有 `EN=` 这样的字样（理论上），所以只认行首那
      三种前缀，且各取第一条。
    """
    out = {"EN": "", "ZH": "", "TW": ""}
    for line in text.splitlines():
        for key in out:
            if line.startswith(f"{key}=") and not out[key]:
                out[key] = line[len(key) + 1:]
    return out


def judge_probe(text: str) -> list[tuple[bool, str]]:
    """④ 它能跑 + ⑤ 中文能显示 —— 从容器输出判。

    这一条是「中文优先」最小的可执行定义：不是文档里说支持中文，而是一个
    真实的程序在这个系统上真的打出了中文。
    """
    got = parse_probe_output(text)
    results: list[tuple[bool, str]] = []

    ok = got["EN"] == "Hello, world!"
    results.append((ok, f"英文输出正确（{got['EN']}）" if ok
                    else f"英文输出不对：[{got['EN']}]"))

    ok = "世界你好" in got["ZH"]
    results.append((ok, f"中文输出正确（{got['ZH']}）" if ok
                    else f"中文没出来：[{got['ZH']}]"
                             f" —— gettext / NLS / locale 三者之一断了"))

    ok = "哈囉" in got["TW"]
    results.append((ok, f"繁体输出正确（{got['TW']}）" if ok
                    else f"繁体没出来：[{got['TW']}]"))
    return results


#: `apk adbdump` 的渲染里那一行：`      mtime: 1791778546`
_MTIME_RE = re.compile(r"^\s*mtime:\s*([0-9]+)\s*$")


def parse_mtimes(dump: str) -> list[int]:
    """`apk adbdump` 的渲染 → 包里所有条目的 mtime。"""
    return [int(m.group(1)) for line in dump.splitlines()
            if (m := _MTIME_RE.match(line))]


def judge_mtime(mtimes: list[int], *, sample: str, now: float | None = None
                ) -> tuple[bool, str]:
    """⑥ 可复现的归一化：一个包里【只该有一种】mtime，而且它不该是刚才那会儿。

    ★ 守的是什么：`.apk` 里所有条目的 mtime 都由 `apk mkpkg` 统一重设成
      `SOURCE_DATE_EPOCH` ——**这是整条链上唯一的归一化**。而它一旦不生效
      （没人传那个变量、或者将来某个包不走 `skirt pack` 自己打包），第②步
      留下的墙钟会**直接进包**，而没有任何别的地方会告诉你。

    ★ 而判据是 `apk adbdump`，**不是"解开看时间戳"**：解包器不恢复符号链接和
      目录的时间戳（apk-tools 的 fs_fsys.c 里，那处 utimensat 只对非符号链接
      调用），所以"解出来时间不对"会假阳性。实测：同一个包解两次、隔 80 秒，
      符号链接的时间跟着走，而真实文件不变。（见 docs/约定/客观性.md）
    """
    now = time.time() if now is None else now
    if not mtimes:
        return False, f"读不出这个包的 mtime（adbdump 有问题？）—— {sample}"

    distinct = set(mtimes)
    if len(distinct) > 1:
        shown = " ".join(str(v) for v in sorted(distinct)[:5])
        return False, (f"一个包里出现了 {len(distinct)} 种 mtime —— 归一化没生效"
                       f"（{shown}）")

    stamp = next(iter(distinct))
    age = now - stamp
    if age < FRESH_WINDOW:
        return False, (f"那个 mtime 就在一小时之内（{stamp}）—— "
                       f"它抄的是墙钟，不是 SOURCE_DATE_EPOCH")
    return True, f"只有一个 mtime（{stamp}），来自 {sample}"


def newest_apk(pkgs_dir: Path) -> Path | None:
    """var/pkgs 里最近打的那个包（原来是 `ls -t | head -1`）。"""
    apks = [p for p in pkgs_dir.glob("*/*.apk") if p.is_file()]
    if not apks:
        return None
    return max(apks, key=lambda p: p.stat().st_mtime)


# ------------------------------------------------------------------ 容器（argv 生成，可测）

def collision_script() -> str:
    """① 里那段【在容器里】跑的脚本：数大小写碰撞的组数。

    ★ 在宿主上解压就会互相覆盖（APFS 默认大小写不敏感，实测 13 组变 0 组），
      所以解压必须在容器里 —— 而这个数字就是"它到底在哪边解的"的证据。
    """
    return (
        "mkdir -p /x && tar -xf /src.tar -C /x\n"
        "python3 - <<PY\n"
        "import pathlib, collections\n"
        "root = next(pathlib.Path('/x').iterdir())\n"
        "low = collections.defaultdict(list)\n"
        "for p in root.rglob('*'):\n"
        "    low[str(p).lower()].append(str(p))\n"
        "print(sum(1 for v in low.values() if len(v) > 1))\n"
        "PY\n")


def _abs(path: Path) -> Path:
    """挂进容器之前先变成绝对路径。

    ★ 这不是讲究：`docker run -v 相对路径:/x` 不会报错 —— 它把那个相对路径当成
      **具名卷的名字**，于是 /x 挂的是一个空卷。症状是"容器里那个文件不见了"
      （adbdump 读不出 mtime、tar 说没有那个包），而离成因很远。
      这条链上原来那几处都是 `$PWD/...` 拼出来的，所以没露出来；换成 Python
      之后就看不出哪个是绝对的、哪个不是了 —— 所以在这里一次性解决。
    """
    return path if path.is_absolute() else path.resolve()


def collision_argv(kernel_tar: Path, image: str) -> list[str]:
    return ["docker", "run", "--rm", "-v", f"{_abs(kernel_tar)}:/src.tar:ro",
            image, "bash", "-c", collision_script()]


def probe_argv(payload: Path, locales: Path, image: str) -> list[str]:
    """④⑤ 在一个【真实的根文件系统】里跑，而不是从挂载点直接跑。

    差别是实的：消息目录的位置是编译进程序的（/usr/share/locale），从别处跑
    就找不到翻译 —— 那样测出来的「中文不显示」是假阳性。
    """
    script = ("cp -a /in/hello/. / ; cp -a /in/loc/. /\n"
              "echo \"EN=$(hello)\"\n"
              "echo \"ZH=$(LANG=zh_CN.UTF-8 LC_ALL=zh_CN.UTF-8 hello)\"\n"
              "echo \"TW=$(LANG=zh_TW.UTF-8 LC_ALL=zh_TW.UTF-8 hello)\"\n")
    return ["docker", "run", "--rm",
            "-v", f"{_abs(payload)}:/in/hello:ro",
            "-v", f"{_abs(locales)}:/in/loc:ro",
            image, "bash", "-c", script]


def adbdump_argv(apk: Path, image: str = APK_IMAGE) -> list[str]:
    return ["docker", "run", "--rm", "-v", f"{_abs(apk)}:/p.apk:ro", image,
            "apk", "adbdump", "/p.apk"]


def build_argv(probe: str, arch: str) -> list[str]:
    """调自己（`skirt build`）—— 冒烟测的就是它。"""
    return [sys.executable, str(ROOT / "skirt" / "skirt"), "build", probe,
            "--arch", arch]


# ------------------------------------------------------------------ 跑

class Report:
    """一行一条结果 —— ✓ / ✗ / 跳过。"""

    def __init__(self, out=print) -> None:
        self.out = out
        self.failed: list[str] = []

    def pass_(self, text: str) -> None:
        self.out(f"  \033[32m✓\033[0m {text}")

    def fail(self, text: str, tag: str) -> None:
        self.out(f"  \033[31m✗\033[0m {text}")
        self.failed.append(tag)

    def note(self, text: str) -> None:
        self.out(f"    {text}")

    def section(self, text: str) -> None:
        self.out(text)


def smoke(arch: str, *, probe: str = PROBE, buildenv: str | None = None,
          kernel_tar: Path | None = None, out=print,
          runner=subprocess.run) -> int:
    """跑六项检查。返回退出码（0 = 这条链此刻是通的）。

    `runner` 可以换掉 —— 测试用它喂罐头输出，把整条编排跑一遍（不开容器）。
    """
    buildenv = buildenv or DEFAULT_BUILDENV
    rep = Report(out)

    out("")
    out(f"  skirt 冒烟测试（{arch}）")
    out("")

    # ── ① 解压完整性 ─────────────────────────────────────────────────────
    rep.section("  ① 源码解压的完整性")
    # ★ 内核 tarball 现在【没有固定的存放处】—— 源码缓存已经移除了（见
    #   source.py 顶部那段）。所以要么用 --kernel-tar 指定一份，要么跳过。
    #
    #   为什么不在这里自动下：它一百多 MB，而这一条的定位是「大约几分钟」。
    tarball = kernel_tar
    if tarball is None:
        found = sorted((BUILD_DIR / "download").glob("linux-*.tar.xz"))
        tarball = found[0] if found else None
    if tarball is None:
        rep.note("跳过：没有内核 tarball（用 --kernel-tar 指定一份）")
    else:
        proc = runner(collision_argv(tarball, f"{buildenv}:{arch}"),
                      capture_output=True, text=True)
        n = parse_collisions(proc.stdout)
        if n == CASE_COLLISIONS:
            rep.pass_(f"内核源码 {CASE_COLLISIONS} 组只差大小写的文件都在"
                      f"（解压发生在容器里）")
        else:
            rep.fail(f"内核源码只找到 {n} 组大小写碰撞，期望 {CASE_COLLISIONS}"
                     f" —— 解压可能在宿主上做了", "unpack")

    # ── ② 端到端构建 ─────────────────────────────────────────────────────
    out(f"  ② 端到端构建 {probe}")
    # 先删掉上一次的产物，否则「构建成功」可能只是在看旧的。
    for old in sorted((OUTPUT_DIR / arch).glob(f"{probe}-*")):
        rmtree_force(old)
    log_file = Path("/tmp/smoke-build.log")
    proc = runner(build_argv(probe, arch), capture_output=True, text=True)
    log_file.write_text((proc.stdout or "") + (proc.stderr or ""),
                        encoding="utf-8")
    if proc.returncode == 0:
        rep.pass_("构建成功")
    else:
        rep.fail(f"构建失败——日志在 {log_file}", "build")
        for line in ((proc.stdout or "").splitlines())[-5:]:
            rep.note(line)
    payload = next(iter(sorted((OUTPUT_DIR / arch).glob(f"{probe}-*/payload"))), None)

    # ── ③ 产物形态 ───────────────────────────────────────────────────────
    rep.section("  ③ 产物形态")
    if payload is None:
        rep.fail("没有产物目录", "payload")
    else:
        count, binary_ok = payload_shape(payload)
        if count == 0:
            rep.fail("产物是空的——「收集成功」不等于「收到了东西」", "empty")
        else:
            rep.pass_(f"产物非空（{count} 项）")
        if binary_ok:
            rep.pass_(f"usr/bin/{probe} 存在且可执行")
        else:
            rep.fail(f"usr/bin/{probe} 缺失或不可执行", "binary")

    # ── ④ 它能跑 + ⑤ 中文能显示 ──────────────────────────────────────────
    rep.section("  ④ 它能跑")
    rep.section("  ⑤ 中文能显示")
    locales = next(iter(sorted((OUTPUT_DIR / arch).glob("glibc-locales-*/payload"))),
                   None)
    if payload is None or locales is None:
        rep.note(f"跳过：缺 {probe} 或 glibc-locales 的产物")
    else:
        proc = runner(probe_argv(payload, locales, f"{buildenv}:{arch}"),
                      capture_output=True, text=True)
        for ok, text in judge_probe(proc.stdout or ""):
            if ok:
                rep.pass_(text)
            else:
                rep.fail(text, "run")

    # ── ⑥ 可复现的归一化 ─────────────────────────────────────────────────
    rep.section("  ⑥ 可复现的归一化")
    sample = newest_apk(PKGS_DIR)
    if sample is None:
        rep.note("跳过：var/pkgs 里没有 .apk（那要有一次 `skirt pack` 才产得出来）")
    else:
        proc = runner(adbdump_argv(sample), capture_output=True, text=True)
        ok, text = judge_mtime(parse_mtimes(proc.stdout or ""),
                               sample=sample.name)
        if ok:
            rep.pass_(text)
        else:
            rep.fail(text, sample.name)

    # ── 结果 ─────────────────────────────────────────────────────────────
    out("")
    if not rep.failed:
        out("  \033[32m全部通过\033[0m —— 这条链此刻是通的")
        out("")
        return 0
    out(f"  \033[31m有失败\033[0m，失败项: {' '.join(rep.failed)}")
    out("")
    return 1
