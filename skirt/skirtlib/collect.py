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
from .buildkey import write_sidecar
from .spec import spec_path

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
            # ★ 符号链接【自己】的 mtime 也要带过来。
            #
            #   `os.symlink()` 建出来的链接，时间戳是"此刻"—— 而真实文件走的是
            #   `copy2`，它连时间戳一起带。于是同一个 payload 里，符号链接和目录
            #   带着墙钟，而文件带的是确定值。
            #
            #   ★ 而那【不影响产物】：`apk mkpkg` 会把所有条目的 mtime 统一重设
            #     成 SOURCE_DATE_EPOCH（实测：设了它，四个只差 mtime 的树产出
            #     完全相同的 .apk）。这一行是为了让**工作树自己**也是确定的 ——
            #     否则任何"比对 var/output 两次构建"的检查都会假阳性。
            #
            #   ★★ 而 `follow_symlinks=False` 不是可选的：默认的 `os.utime`
            #      改的是**链接指向的那个文件**，链接自己不动。实测差别是
            #      2020 vs now。这和 build.py 里"chown 顺手清掉 setuid"是同一族
            #      的坑：**默认行为作用在另一个对象上。**
            try:
                st = src.lstat()
                os.utime(dst, ns=(st.st_atime_ns, st.st_mtime_ns),
                         follow_symlinks=False)
            except (OSError, NotImplementedError):
                # 有些平台/文件系统不支持。**而它不影响产物**（见上），所以不拦。
                pass
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


def collect(spec: dict, arch: str,
            build_key: dict | None = None,
            epoch: int | None = None) -> list[tuple[Path, int]]:
    """收集一个配方的全部产出，返回 [(目录, 文件数), ...]，主包在前。

    `build_key` 是 `buildkey.compute()` 算出来的那个 dict（{"key", "parts"}）。
    它写进 meta.json，也在产物目录里留一份旁路文件 —— 为什么见 buildkey.py。

    `epoch` 是这个包的 SOURCE_DATE_EPOCH（`deps.source_date_epoch()` 算的那个）。
    它写进 meta.json，而 **`skirt pack` 拿它当 `apk mkpkg` 的时间戳** —— 也就是
    决定 `.apk` 字节的那个值。见下面那段。
    """
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

            # ★ source_date_epoch —— 而不是"现在几点"。
            #
            #   这里原来是 `time.strftime("%Y-%m-%dT%H:%M:%S%z")`，也就是【墙上
            #   时钟】。而它有两个后果，都是这一格踩出来的：
            #
            #     一、`skirt pack` 拿它当 `apk mkpkg` 的 SOURCE_DATE_EPOCH →
            #         同一个配方建两次，.apk 字节不同。**0005 定的"可复现构建"
            #         在产物上没成立。** 实测：发布出去那个 zlib 包里文件的时间
            #         是 2026-10-15（构建时刻），而配方 date 是 2026-10-10。
            #
            #     二、`%z` 带本机时区 → 换个时区建，值又不同。
            #
            #   改用 `deps.source_date_epoch()` 算的那个值（= max(配方日期,
            #   依赖的日期)），而 `skirt build` 本来就在用它喂构建 —— 只是没
            #   传到这里来。
            #
            #   ★ 而字段名也一起改了。原来叫 `built_at`，字面意思是"什么时候
            #     建的" —— 而那恰恰是不可复现的那一半。**名字该说的是它是
            #     什么，而不是让人以为它是别的。**
            "source_date_epoch": int(epoch) if epoch is not None else None,
            "files": entries,
        }
        if provides:
            meta["provides"] = list(provides)
        if priority is not None:
            meta["provider_priority"] = priority

        # ★ build_key —— 这个包的身份，从内容算出来。见 buildkey.py 顶上那段。
        #
        #   两个地方都写：
        #     meta.json        给人看、给 skirt pack 用
        #     .buildkey        旁路文件，跟着 var/pkgs 的缓存跨 CI 运行走
        #
        #   为什么需要旁路文件：`apk mkpkg --info` 不收自定义字段
        #   （实测 "ERROR: invalid info field: build_key"），所以 key 进不了
        #   .apk。而 CI 恢复的是 var/pkgs 里的 .apk，meta.json 不跟着走。
        if build_key is not None:
            meta["build_key"] = build_key["key"]
            meta["build_key_parts"] = build_key["parts"]

        (out / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        if build_key is not None:
            write_sidecar(out, build_key)

        # ── 安装脚本（post-install / post-upgrade / …）──────────────────────
        #
        # ★ 为什么需要这条通路：apk 的包除了文件树，还能带几个"在装/卸的某个
        #   时刻跑一下"的脚本，而 `apk mkpkg --script <类型>:<路径>` 就是喂它的
        #   方式。
        #
        #   而为什么现在才加：openssh 那个 `/root/.ssh/authorized_keys` 从
        #   "归包所有"改成"不归包所有"之后，**升级路径上它会被 apk 删掉**
        #   （规矩是"旧版本有、新版本没有的文件删掉"）—— 实测过了：
        #
        #       装 r4       →  /root/.ssh/authorized_keys 在
        #       升级到 r6   →  /root 下【空了】
        #
        #   而那个文件不在 = 谁也进不去（要等下次开机钩子补回来）。所以需要
        #   一个"装完立刻补上"的钩子，也就是 post-install。
        #
        #   约定：配方目录下的 `scripts/` 里，**文件名就是 mkpkg 的脚本类型**
        #   （post-install、post-upgrade、pre-install…）。这样不用再发明一层
        #   配置——名字本身就是类型。
        rdir = spec_path(pkg["name"])
        if rdir:
            sd = rdir.parent / "scripts"
            if sd.is_dir():
                (out / "scripts").mkdir(exist_ok=True)
                for f in sorted(sd.iterdir()):
                    if f.is_file():
                        shutil.copy2(f, out / "scripts" / f.name)
        results.append((out, len(entries)))

    return results
