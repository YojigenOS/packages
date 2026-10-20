#!/usr/bin/env python3
"""看一个 apk 仓库自洽不自洽 —— 以及断言某个包链了什么。

    ./scripts/check-repo.py
    ./scripts/check-repo.py --arch aarch64
    ./scripts/check-repo.py --repo https://repo.os.yojigen.cn
    ./scripts/check-repo.py --repo var/repo          # 本地目录也行
    ./scripts/check-repo.py --expect-so readline=so:libncursesw.so.6

## 它为什么存在

2026-10-20 那个 `undefined symbol: UP`：`libreadline.so.8` 里对 termcap 的
符号有引用，而那个 `.so` 的 **NEEDED 里没有 libncursesw** —— 于是任何动态链
它的东西（python3 的 readline 模块）都跑不起来，而 `python3` 连着五轮 CI 失败。

★ 而那件事【依赖检查看不见】：

    方向 1  声明了而没人提供      ← 这个脚本查的
    方向 2  链了而没声明          ← `pack.elf_fields` 从 NEEDED 自动补
    方向 3  该链而没链            ← 没人查；只有这一条能抓住 readline 那种

所以这个脚本单靠自己是抓不到那个 bug 的。它要做的是**另外两件事**：

* 把方向 1 变成一条命令（原先要手工写 awk 读 index，那天我做了两遍）
* `--expect-so` 让"某包必须链某库"变成一个**能失败的断言** —— 那才是对着
  方向 3 的判据，而它需要人先把"该链什么"说出来（上一个是和 Alpine 对照出来的）

## 它为什么是 `.py` 而不是 `.sh`

`scripts/**/*.sh` 会进 `tooling_hash` —— 改它就**让每个包的 build_key 都变**，
于是触发一次全量重建（2 小时 / 架构）。`*.py` 不在里面。

★ 实测过：在 `scripts/` 下加一个 `.py` → `tooling_hash` 不变；加一个 `.sh` → 变。
  所以工具放 `.py`，而**构建用的** shell 才放 `.sh`。
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from collections import defaultdict
from pathlib import Path

DEFAULT_REPO = "https://repo.os.yojigen.cn"


def die(msg: str) -> None:
    print(f"✗ {msg}", file=sys.stderr)
    raise SystemExit(2)


def fetch_index(repo: str, arch: str) -> bytes:
    """拿到 APKINDEX.tar.gz 的内容（字节）。`repo` 可以是 URL 或本地目录。"""
    if "://" in repo:
        url = f"{repo.rstrip('/')}/{arch}/APKINDEX.tar.gz"
        print(f"  仓库 {url}")
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001 - 报原样，别包一层
            die(f"取不到 {url}\n       {e}")
    p = Path(repo) / arch / "APKINDEX.tar.gz"
    if not p.is_file():
        die(f"找不到 {p}")
    print(f"  仓库 {p}")
    return p.read_bytes()


def adbdump(blob: bytes) -> str:
    """把 ADB 格式的 index 转成人能读的 YAML。

    v3 的 index 是 ADB（`ADBd` 魔数），不是 tar.gz。而解析它只需要 `apk
    adbdump` —— 所以有 `apk` 就用，没有就用 alpine 容器里的。
    """
    tmp = tempfile.NamedTemporaryFile(suffix=".adb", delete=False)
    try:
        tmp.write(blob)
        tmp.close()
        if shutil.which("apk"):
            out = subprocess.run(["apk", "adbdump", tmp.name],
                                 capture_output=True, text=True)
            if out.returncode == 0:
                return out.stdout
        if not shutil.which("docker"):
            die("既没有 apk 也没有 docker —— 解析不了这个 index")
        out = subprocess.run(
            ["docker", "run", "--rm", "-v", f"{tmp.name}:/i:ro", "alpine:latest",
             "sh", "-c", "apk adbdump /i 2>/dev/null"],
            capture_output=True, text=True)
        if out.returncode != 0 or not out.stdout.strip():
            die(f"docker + apk adbdump 也失败了：{(out.stderr or '')[:200]}")
        return out.stdout
    finally:
        os.unlink(tmp.name)


def parse(yaml_text: str) -> list[dict]:
    """按 `- name:` 切块。够用就好 —— 这不是一个通用的 YAML 解析器。"""
    pkgs = []
    # ★★★ `apk adbdump` 的每一块**不以换行结尾**（实测 124 块里 123 块如此）。
    #
    #   而 `depends` / `provides` 总是块里的最后一节 —— 于是下面那个
    #   `(?:      - .*\n)+` 会**吞掉每块的最后一个依赖 / 提供**。
    #
    #   zlib 只有一个 provide（`so:libz.so.1=3.2`），于是它的 provides 解析成
    #   空集，而"so:libz.so.1 没人提供"就冒出来了 —— 一个看起来非常像真的假
    #   结论（libssl / libz / libstdc++ 全都"没人提供"，哪个仓库会那样）。
    #
    #   2026-10-20 现场抓到的，就在这个脚本自己身上。
    #
    #   ★ 而第一版补丁只在【整个文本】末尾补 —— 没用，因为分隔符里的那个 `\n`
    #     已经被 `re.split` 吃掉了，于是【每一块】都不以换行结尾。要在
    #     【每一块】上补。
    for block in re.split(r"\n(?=  - name: )", yaml_text):
        # ★ 交给下面的每一块都补一个换行（见上面那一大段的理由）。
        block += "\n"
        m = re.search(r"^  - name: (\S+)", block, re.M)
        if not m:
            continue
        ver = re.search(r"^    version: (\S+)", block, re.M)

        def section(tag: str) -> set[str]:
            # ★★★ 行尾可能是 `depends:` 也可能是 `depends: # 3 items` —— 后者
            #   是 `apk adbdump` 实际打的。第一版正则只认前者，于是【每一条都
            #   解析成空集】，而"没人提供的依赖 0 个"就永远成立。
            #
            #   2026-10-20 现场抓到的：123 条里"有 depends 的 0 条"。
            #   **一个静默读空的检查，比没有检查更糟** —— 它会给出一个结论。
            mm = re.search(rf"^    {tag}:\s*(?:#[^\n]*)?\n((?:      - .*\n)+)",
                           block, re.M)
            return {x.strip()[2:] for x in mm.group(1).splitlines()} if mm else set()

        pkgs.append({
            "name": m.group(1),
            "version": ver.group(1) if ver else "?",
            "depends": section("depends"),
            "provides": section("provides"),
        })
    return pkgs


def main() -> int:
    ap = argparse.ArgumentParser(description="看一个 apk 仓库自洽不自洽")
    ap.add_argument("--repo", default=DEFAULT_REPO,
                    help=f"仓库 URL 或本地目录（默认 {DEFAULT_REPO}）")
    ap.add_argument("--arch", default="x86_64")
    ap.add_argument("--expect-so", action="append", default=[],
                    metavar="包=so:名字",
                    help="断言某个包的 depends 里有某个 so:（可给多次）。"
                         "★ 这是对着「该链而没链」那条的判据 —— 要人先说出该链什么")
    ap.add_argument("--quiet", action="store_true", help="只报结论")
    a = ap.parse_args()

    pkgs = parse(adbdump(fetch_index(a.repo, a.arch)))
    if not pkgs:
        die("解析出 0 个包 —— index 格式变了？")
    names = {p["name"] for p in pkgs}
    # ★★★ provides 里的 so: 可能带版本、depends 里的不带：
    #
    #       provides:  so:libz.so.1=1.3.1
    #       depends:   so:libz.so.1
    #
    #   所以要先按 `=` 去掉尾巴再比。第一版直接拿整个字符串当键，于是【所有】
    #   so: 依赖都被判成"没人提供" —— 而那个假结论看起来非常像真的
    #   （libssl / libz / libstdc++ 全都"没人提供"，一个仓库怎么会那样）。
    #
    #   2026-10-20 现场抓到的：`apk add` 明明装得上 65 个包，而脚本说几十个依赖
    #   没人提供 —— **两个结论矛盾的时候，信那个能复现的。**
    provided: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for p in pkgs:
        for x in p["provides"]:
            if x.startswith("so:"):
                provided[x.split("=", 1)[0]].add((p["name"], p["version"]))

    bad = 0

    # ── 方向 1：声明了而没人提供 ──────────────────────────────────────────
    missing: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for p in pkgs:
        for x in p["depends"]:
            if x.startswith("so:"):
                if x not in provided:
                    missing[x].append((p["name"], p["version"]))
            elif "=" not in x and not x.startswith(("cmd:", "pc:")) and x not in names:
                missing[x].append((p["name"], p["version"]))

    print(f"  {len(pkgs)} 条 · 其中 {len(names)} 个不同的包名")
    if missing:
        bad += 1
        print(f"\n  ✗ 没人提供的依赖 {len(missing)} 种：")
        for x, who in sorted(missing.items()):
            by = ", ".join(f"{n}-{v}" for n, v in who[:4])
            print(f"      {x:<34} ← 被 {len(who)} 个包要：{by}"
                  f"{' …' if len(who) > 4 else ''}")
    else:
        print("  ✓ 每个声明的依赖都有人提供")

    # ── 方向 3 的判据：某包必须链某库 ────────────────────────────────────
    for spec in a.expect_so:
        if "=" not in spec:
            die(f"--expect-so 要 `包=so:名字`，收到 {spec!r}")
        want_pkg, want_so = spec.split("=", 1)
        hit = [p for p in pkgs if p["name"] == want_pkg]
        if not hit:
            bad += 1
            print(f"\n  ✗ 仓库里没有 {want_pkg}")
            continue
        for p in hit:
            ok = want_so in p["depends"]
            mark = "✓" if ok else "✗"
            note = "" if ok else f"   （它的 depends：{sorted(p['depends']) or '空'}）"
            print(f"  {mark} {p['name']}-{p['version']} 链 {want_so}？{note}")
            if not ok:
                bad += 1

    print()
    if bad:
        print(f"  ✗ {bad} 处不对")
        return 1
    print("  ✓ 都对了")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
