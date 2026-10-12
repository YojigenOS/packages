"""产物收集：把 $PKGDEST 收成 var/output/<架构>/<包>-<版本>-<release>/。

一个配方可以产出多个包：主包加上 `[[subpackage]]` 声明的那些。拆分发生在
**这里**，不在构建脚本里——构建照旧把一切装进 $PKGDEST，所以调整拆分规则
只要重跑收集，不用重编（glibc 重编一次是 8 分钟）。

分流靠文件的**路径模式**，第一个匹配的子包获胜，谁都不匹配就归主包。
子包在下游看起来就是普通包，`dep_payload` 之类不需要知道它的存在。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
from functools import lru_cache
from pathlib import Path

from .common import BUILD_DIR, OUTPUT_DIR, die, rmtree_force, sha256_of

from .spec import deps_for, package_rel, subpackages

# ------------------------------------------------------------------ 模式匹配

@lru_cache(maxsize=512)
def _glob(pat: str) -> re.Pattern:
    """把 glob 转成正则：`*` 不跨 `/`，`?` 匹配单个非 `/` 字符。

    不用 fnmatch：它的 `*` 连 `/` 一起吃掉，于是 `usr/lib/*.a` 会命中
    `usr/lib/sub/libc.a`——一个模式能意外吞掉整棵子树，而分流错了的后果
    是文件跑到别的包里，产出一个装得上、跑不起来的系统。
    """
    out = []
    for ch in pat:
        if ch == "*":
            out.append("[^/]*")
        elif ch == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(ch))
    return re.compile("^" + "".join(out) + "$")


def _pattern_match(rel: str, pat: str) -> bool:
    """判断相对路径 rel 是否被模式 pat 覆盖。

    支持三种写法：

        usr/include/        目录——它自己，以及底下的一切
        usr/lib/*.a         glob，`*` 不跨 `/`
        usr/lib/libc.so     精确路径
    """
    if pat.endswith("/"):
        prefix = pat[:-1]
        return rel == prefix or rel.startswith(prefix + "/")
    return _glob(pat).match(rel) is not None


def _owner(rel: str, subs: list[dict]) -> str | None:
    """这个路径归哪个子包；归主包则返回 None。第一个匹配的获胜。"""
    for sub in subs:
        if any(_pattern_match(rel, pat) for pat in sub["files"]):
            return sub["name"]
    return None


# ------------------------------------------------------------------ 收集

def _entries(root: Path) -> list[Path]:
    """源树里的一切，按路径排序。"""
    return sorted(root.rglob("*"))


def _describe(p: Path, base: Path) -> dict:
    rel = str(p.relative_to(base))
    if p.is_symlink():
        return {"path": rel, "type": "symlink", "target": os.readlink(p)}
    if p.is_dir():
        return {"path": rel + "/", "type": "dir"}
    return {"path": rel, "type": "file", "size": p.stat().st_size, "sha256": sha256_of(p)}


def _write_package(out: Path, destdir: Path, wanted: set[str]) -> None:
    """把 wanted 里的路径从 destdir 复制到 out/payload。

    父目录按需补出来——一个文件被分到子包，它的上级目录可能一个都没跟过来
    （比如 usr/lib/libc.a 走了，usr 和 usr/lib 也要在新包里存在）。
    """
    rmtree_force(out)
    payload = out / "payload"
    payload.mkdir(parents=True, exist_ok=True)

    for rel in sorted(wanted):
        src = destdir / rel
        dst = payload / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_symlink():
            if dst.is_symlink() or dst.exists():
                dst.unlink()
            dst.symlink_to(os.readlink(src))
        elif src.is_dir():
            dst.mkdir(parents=True, exist_ok=True)
            # ★ 目录的模式也要带过来。
            #
            # mkdir 用默认的 0755，于是 /tmp 的 1777 和 /root 的 0700 全丢了。
            # 文件那一路有 shutil.copy2 兜着，只有目录会中招——所以
            # home/etc/var 看着是对的（它们本来就是 0755），而 tmp/var/tmp/root
            # 全错。
            #
            # 代价实测过：本地自举跑到最后一个包 glibc，configure 报
            #
            #     config.guess: cannot create a temporary directory in /tmp
            #
            # 因为构建是【降权】跑的，而 /tmp 成了 0755。
            #
            # merge_tree（deps.py）里早就写着同一件事——"丢了这些模式，构建会
            # 以莫名其妙的 Permission denied 失败"。它那里修了，这里漏了。
            shutil.copystat(src, dst)
        else:
            shutil.copy2(src, dst, follow_symlinks=False)


def collect(spec: dict, arch: str) -> list[tuple[Path, int]]:
    """收集一个配方的全部产出，返回 [(目录, 文件数), ...]，主包在前。"""
    pkg = spec["package"]
    destdir = BUILD_DIR / "dest"
    subs = subpackages(spec)

    if not destdir.is_dir():
        die(f"找不到构建产物目录: {destdir}")

    # 先把整棵树的归属算出来，再统一复制。这样「有没有文件掉了」是可以
    # 直接验证的，而不是靠事后拼凑。
    buckets: dict[str | None, list[dict]] = {None: []}
    for sub in subs:
        buckets[sub["name"]] = []

    seen_files: set[str] = set()
    for p in _entries(destdir):
        rel = str(p.relative_to(destdir))
        entry = _describe(p, destdir)
        buckets[_owner(rel, subs)].append(entry)
        # 用 _describe 已经分好的类，不要另写一遍判断。Path.is_dir() 会
        # 【跟随符号链接】，于是 bin → usr/bin 这类链接被当成目录，和
        # _describe 的结论不一致——「一个文件都没有丢」这条检查当场就炸了。
        if entry["type"] != "dir":
            seen_files.add(rel)

    # 一个文件只能属于一个包。真有文件掉了，这里立刻炸，而不是产出一个
    # 看起来正常、运行时才发现少东西的系统。
    placed = {f["path"] for files in buckets.values() for f in files if f["type"] != "dir"}
    if placed != seen_files:
        missing = sorted(seen_files - placed)
        extra = sorted(placed - seen_files)
        die(
            "收集时文件归属不一致：\n"
            f"       没有归处的: {missing[:5]}\n"
            f"       重复出现的: {extra[:5]}"
        )

    deps = deps_for(spec, arch)
    runtime = list(deps.get("runtime", []))
    results: list[tuple[Path, int]] = []

    for sub in [None] + subs:
        entries = buckets[sub["name"] if sub else None]
        if sub is None:
            name, out_dir = pkg["name"], pkg
            desc = pkg.get("description", "")
            section = pkg.get("section", "")
            lic, url = pkg.get("license", ""), pkg.get("url", "")
            sub_depends = runtime
        else:
            name = sub["name"]
            desc = sub.get("description", "")
            section = sub.get("section", pkg.get("section", ""))
            lic, url = pkg.get("license", ""), pkg.get("url", "")

            # ── ★ 子包【不】依赖主包 ──────────────────────────────────────
            #
            # 原来这里是 `sub_depends = [pkg["name"]] + [...]`，注释写着
            # 「子包依赖主包：它的文件是主包的一部分，单独装没有意义」。
            #
            # **那个前提在构建期成立，在安装语义上是反的。** 子包的文件确实
            # 是从主包的 $PKGDEST 分出来的，但分完之后主包的 payload 里
            # 【已经没有了】——那是分流的意思。所以：
            #
            #     libcrypto 的文件不在 openssl 里面
            #     libssl    的文件不在 openssl 里面
            #     libgcc    的文件不在 gcc 里面
            #
            # 装子包不需要主包；反过来"单独装没有意义"才是错的——单独装正是
            # 子包存在的理由。
            #
            # ★ 实测代价：openssl 主包 17 MB、gcc 主包 253 MB。带着那条依赖，
            #   装 libssl 会拖进 17 MB，装 libgcc 会拖进 253 MB——而那正好是
            #   拆子包要避免的事。
            #
            # ★ 对比 Alpine：它的 libgcc 只依赖 musl，不依赖 gcc。
            #
            # ★ 可以覆盖：[[subpackage]] 写 depends 就用它的。默认继承主包的
            #   runtime（过度但保守），因为子包通常需要主包依赖里的某几样
            #   （libcrypto 要 glibc）。
            if "depends" in sub:
                sub_depends = list(sub["depends"])
            else:
                sub_depends = [d for d in runtime if d != pkg["name"]]

        # ── provides / provider-priority ────────────────────────────────
        #
        # 「这个包提供什么【路径或虚名】」。apk 用它解析对虚包的依赖。
        #
        # 为什么需要它：/bin/sh 这种东西，哪个包提供【不该写死在某个包里】。
        # 我们原来让 filesystem 提供 /bin/sh → bash。那意味着所有系统都只能
        # 是 bash，换不了。而 Alpine 的做法是：
        #
        #     dash-binsh     provides="/bin/sh"  provider-priority=60
        #     bash-binsh     provides="/bin/sh"  provider-priority=…
        #     busybox-binsh  provides="/bin/sh"  provider-priority=…
        #
        # 装哪个由【风味】决定，不装就没有。
        #
        # ★ provider-priority 是【数字大的优先】（实测：90 赢了 60 和 30）。
        #   名字里带横线——mkpkg 收 provider-priority，不收 provider_priority。
        src = sub if sub else pkg
        provides = src.get("provides") or []
        priority = src.get("provider-priority", src.get("provider_priority"))

        out = OUTPUT_DIR / arch / package_rel(name, pkg["version"], pkg["release"])
        _write_package(out, destdir, {e["path"].rstrip("/") for e in entries})

        meta = {
            "name": name,
            "version": pkg["version"],
            "release": pkg["release"],
            "arch": arch,
            "description": desc,
            "section": section,
            "license": lic,
            "url": url,
            "depends": sub_depends,
            "build_depends": deps.get("build", []),
            "built_from": pkg["name"],
            "built_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "files": entries,
        }
        if provides:
            meta["provides"] = list(provides)
        if priority is not None:
            meta["provider_priority"] = priority
        (out / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        results.append((out, len(entries)))

    return results
