"""把一个【已经建好的】包打成 .apk。

    skirt pack hello                  # 按名字找最新的产物
    skirt pack hello glibc bash       # 打几个
    skirt pack --all                  # 打全部
    skirt pack --all --arch x86_64
    skirt pack --all --sign-key ~/.yojigenos/keys/<密钥>.rsa

── 建包和打包是两件事 ────────────────────────────────────────────────────

    skirt build <包>    配方 → var/output/<arch>/<包>-<版本>-<发布号>/
                                  ├── payload/     文件树
                                  └── meta.json    身份 + 依赖 + 日期
    skirt pack           → var/pkgs/<arch>/<包>-<版本>-r<发布号>.apk

── 为什么不用自己实现（apk-tools 已经有全套）────────────────────────────

    apk mkpkg    创建包（v3 格式：魔数是 "ADB"，不是 tar.gz）
    apk adbsign  签名 / 重签
    apk mkndx    创建仓库索引
    apk manifest 读包的校验和
    apk extract  解包

这个模块唯一有技术含量的事是【meta.json → mkpkg 的参数】。其余的字节我们一行
都不碰——v3 格式长什么样不需要知道，因为生成用 mkpkg、读用 apk。

── 为什么用 alpine:latest 而不是 ${BUILDENV} ─────────────────────────────

打包不需要编译器。而 devel 里也没有 apk-tools——它是我们自举出来的 glibc 系统，
apk-tools 要等 5.8 那条链（perl → openssl → curl → apk-tools）。

apk-tools 在这里是【构建期的工具】，不进交付物。用 Alpine 的现成版本正是 0006
说的「只借格式和工具，不借包」。

── mkpkg 的坑（都是试出来的）────────────────────────────────────────────

  · `--info` 用【冒号】分隔：`--info name:hello`
    写成 `name=hello` 报 "missing key or value"，写成两个参数也一样
  · 必填只有 name 和 version；version 要含发布号（2.12.3-r1）
  · 空值不要传——mkpkg 对空字符串会报错

── 它是从 shell 搬过来的（2026-10-15）────────────────────────────────────

原来是 `scripts/pack.sh`（442 行，其中一半是注释）。搬的理由和其它三样一样：
**判定写在 shell 里就测不到**。现在"打哪些目录、时间戳取哪个值、mkpkg 收哪些
字段、旁路文件叫什么名字"全在 Python 里，`skirt/tests/test_pack.py` 守着它们；
容器里只剩「把 payload 摆好（属主/权限/能力位）然后 mkpkg」这一件事。

★ 搬的时候有几处【行为收窄】，逐条记在这里（也都进了上面的测试）：

  ① 那段 `getcap → /tmp/.caps` 的记账 + 恢复（在 `chown -R 0:0` 前后）原来
     **被复制了两遍**，第二遍的末尾读的是 `/in/caps.txt`——而那个文件
     **全项目没有任何地方写它**（`build.py` 写的是 payload 里的
     `.yojigen-caps`，那是唯一的来源）。所以第二遍是更新到一半的残留：
     它多跑一次 chown、多跑一轮 setcap，净效果为零，却会打出一句
     「⚠ 没有 /in/caps.txt」把人往错的方向指。这里只留第一遍。
     ★ 主路（`.yojigen-caps` 清单）在两遍里都执行过，所以去掉的不是判据。

  ② `-e SOURCE_DATE_EPOCH=$(cat "$PACKDIR/epoch" 2>/dev/null || echo 0)`
     —— 那个 `|| echo 0` 是 shell 的写法逼出来的（值要落成文件才能跨进
     `docker run` 的参数里）。它正好是这份代码里最不该有的东西：**拿不到
     时间戳就退化成 0**。现在那个值留在 Python 进程里，根本没有"读不到"这
     条路（拿不到时间戳的包在上面就停住了，见 `epoch_of`）。

  ③ 失败时原来打的那句「退出码: $?」**不是 docker 的退出码** —— 它是上一
     句 `[ -s "$WORK/log" ]` 的（=1）。现在打的是真的那个。

  ④ 其余逐行等价：同一批 mkpkg 字段、同一个 `.buildkey` 旁路文件命名、同一
     套能力位恢复、同一种"退出码为 0 不等于产出了东西"的双重检查、同样
     每个包一个独立的 pack 目录（那个 macOS 绑定挂载的坑，见 `pack_dir`）。
"""

from __future__ import annotations

import datetime
import json
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import elfdeps
from .common import OUTPUT_DIR, PKGS_DIR, ROOT, die

#: 打包用的镜像。apk-tools 在它里面（宿主上是没有 apk 的）。
PACK_IMAGE = "alpine:latest"

#: 签名密钥在容器里挂到哪
KEY_IN_CONTAINER = "/key.rsa"

#: build_key 的旁路文件后缀。★ `apk mkpkg --info` 不收自定义字段
#: （实测 `--info build_key:abc` 报 "ERROR: invalid info field: build_key"），
#: 所以它只能和 .apk【并排】放，名字是 `<全名>.apk` + 这个后缀。
#:
#: ★★ 而 `.apk` 必须留在名字【中间】。上一版写过剥掉 `.apk` 的名字
#:    （`<全名>.buildkey`），于是"还原 payload"那一步（它按 `<全名>.apk` +
#:    后缀找）找不到，症状是「跳过判断说盘上没有 key」——**离成因差三个字符，
#:    而且完全静默**。见 restore.py 顶上 ① 与 docs/约定/客观性.md。
BUILDKEY_SUFFIX = ".buildkey"

#: 打完的包里那个安装脚本目录（collect.py 从配方的 scripts/ 收过来的）
SCRIPTS_DIRNAME = "scripts"


class PackError(Exception):
    """打包这件事本身停住了（不是"某个包打失败"，是"再往下打没意义"）。"""


@dataclass
class Job:
    """一个包的打包装配单 —— 全是算出来的，还没碰容器。"""

    product: Path          #: var/output/<arch>/<包>-<版本>-<发布号>
    meta: dict             #: 它的 meta.json
    name: str
    version: str           #: `<版本>-r<发布号>`（mkpkg 要的 shape）
    apk: str               #: `<名字>-<版本>-r<发布号>.apk`
    epoch: str = ""        #: SOURCE_DATE_EPOCH（字符串）；pack 时填

    @property
    def payload(self) -> Path:
        return self.product / "payload"

    @property
    def scripts(self) -> Path:
        return self.product / SCRIPTS_DIRNAME


# ------------------------------------------------------------------ 选产物

def _shown(path: Path) -> Path:
    """打印用的路径：能相对就相对（和 `skirt repo` 那边的写法一致）。"""
    try:
        return path.relative_to(ROOT)
    except ValueError:
        return path


def _natural_key(text: str) -> tuple:
    """数字段按数字比 —— `sort -V` 的近似。

    ★ 为什么不能直接 `sorted()`：那个是字典序，于是 `-r9` 排在 `-r10` 后面。
      实测会碰到的形状：同一个包连打三次得到 r1…r10（gcc 那种要改好几轮的）。
      而"取最新那个"选错的后果不是报错，是**打出一个旧版本、还报 ✓**。

    拿不到的保证：它不是 GNU 的 `sort -V` 全部规则（`1.0a` / `_p1` 那套 apk
    的版本规则不在里面）。所以它只用来排序【同一个包名】的产物目录——那些
    目录的版本段来自同一个配方，形状一致。真正要比较 apk 版本的地方是
    `skirt prune`，那边问 apk 自己（见 prune.py 顶上那段）。
    """
    return tuple(int(p) if p.isdigit() else p for p in re.split(r"([0-9]+)", text))


def select_products(arch: str, names: list[str] | None = None,
                    *, output_dir: Path = OUTPUT_DIR) -> list[Path]:
    """要打哪些产物目录。

    不指定名字就是 `--all`：`var/output/<arch>/` 下所有目录（按名字排，让输出
    稳定）。指定了名字就只认那些名字。

    ★ 名字后面必须跟【数字】，否则 `hello` 会匹配到 `hello-world`。这条是从
      shell 版的 glob `<名字>-[0-9]*` 原样搬过来的。

    ★ 同一个名字有多个版本时取【最新】的那个 —— `skirt build hello` 建出来的
      最新一份就是它该打的那份。
    """
    base = output_dir / arch
    if not base.is_dir():
        return []
    if not names:
        return sorted((p for p in base.iterdir() if p.is_dir()),
                      key=lambda p: p.name)
    out: list[Path] = []
    for want in names:
        found = [p for p in base.glob(f"{want}-[0-9]*") if p.is_dir()]
        if found:
            out.append(max(found, key=lambda p: _natural_key(p.name)))
    return out


# ------------------------------------------------------------------ meta.json → 装配单

def apk_name(meta: dict) -> str:
    """`<名字>-<版本>-r<发布号>.apk` —— 文件名和产物目录名只差一个 `.apk`。

    （同一个形状在 restore.py 里也出现过：那边的"目录名 → 文件名"靠它。）
    """
    return f"{meta['name']}-{meta['version']}-r{meta['release']}.apk"


def sidecar_name(apk: str) -> str:
    """`.buildkey` 旁路文件的名字：`<全名>.apk.buildkey`。

    ★ `.apk` 在中间（不是剥掉它）——见 BUILDKEY_SUFFIX 的注释：剥掉它那次
      让"还原 payload"静默失效过，而症状离成因很远。
    """
    return apk + BUILDKEY_SUFFIX


def plan(product: Path) -> Job | None:
    """读产物目录的 meta.json → 装配单。没有 meta.json 就 None（调用方跳过它）。"""
    meta_file = product / "meta.json"
    if not meta_file.is_file():
        return None
    meta = json.loads(meta_file.read_text(encoding="utf-8"))
    return Job(product=product, meta=meta, name=meta["name"],
               version=f"{meta['version']}-r{meta['release']}",
               apk=apk_name(meta))


def pack_dir(work: Path, product: Path) -> Path:
    """这个包自己的 pack 目录（`inner.sh` 挂给它）。

    ★★ 每个包一个【唯一的】目录，不是所有包共用一个。

    原来是所有包共用一个 `$WORK/pack`，里面的 inner.sh 每个包重写一次、而挂载
    点始终是同一个宿主路径。实测在 macOS 上会读到【上一个包】的内容：

        /pack/inner.sh: line 13: syntax error: unterminated quoted string

    而那个文件在宿主上是对的（字段数不同的包写到第 13 行就断了）。所以是 bind
    mount 对同一路径的快速重写没跟上。

    独立的症状：失败的是【随机的包】——每跑一次换一批，而单独打某个包总是成功。
    因为单独跑时那个路径上没有"上一次"。

    ★ 用包名而不是 tmp 目录名：出问题时路径里能看出是哪个包。
    """
    return work / "pack" / product.name


def epoch_of(meta: dict, *, note=print) -> str:
    """`meta.json` → `SOURCE_DATE_EPOCH`（字符串）。**拿不到就抛出，不糊。**

    ── 为什么它不问 `--info` 要 ──────────────────────────────────────────

    mkpkg 不认 builddate 这个字段（`ERROR: invalid info field: builddate`）。
    它认的是【环境变量 SOURCE_DATE_EPOCH】——正是 0005 从第一天就在用的那个
    通用约定。实测：同一个包换一个时间戳，产出字节就不同；不设的话它用当前
    时间，于是同样的输入会产出不同的包（xz 连打三次得到 620941/620941/620943）。

    ── 为什么它必须是确定性的 ────────────────────────────────────────────

    那个值决定 .apk 的字节。这里原来读的是 meta.json 的 `built_at`，而那句
    注释写着"它已经是确定性的：max(配方声明的日期, 依赖的日期)"。**那句话是
    错的** —— `built_at` 当时是墙上时钟（`time.strftime`），所以：

        · 同一个配方建两次，.apk 字节不同 —— 0005 的目标在产物上没成立
        · 而且 `%z` 带本机时区，换个时区建又不同

    实测：发布出去那个 zlib 包里文件的时间是 2026-10-15（构建时刻），而配方
    date 是 2026-10-10。

    现在 meta.json 里叫 `source_date_epoch`，是个整数，由
    `deps.source_date_epoch()` 算出来 —— 也就是 `skirt build` 本来就在用来喂
    构建的那个值。两边同一个来源。

    ── 老产物那条退路 ────────────────────────────────────────────────────

    只有 `built_at` 的产物留一条路读它（不然历史产物全部打不出包），**但要把
    "我用了退路"说出来** —— 那意味着那个包的时间戳本来就不对。

    ── 而两条都没有的时候 ────────────────────────────────────────────────

    **响亮地失败，不"用 0"糊过去** —— 那是【静默地产出一个时间戳全错的可复现性
    破口】。

    为什么值得为它停下来：.apk 的可复现**只有这一个开关**（`apk mkpkg` 会把所有
    条目的 mtime 重设成它，链上没有第二处归一化）。所以拿不到它的那个包，字节
    就一定不可复现，而没有任何别的地方会告诉你。

    实测过这一点（2026-10-15，控制变量）：设了 SOURCE_DATE_EPOCH 的四个只差
    mtime 的树产出【完全相同】的 .apk；不设则四个互不相同。
    """
    epoch = meta.get("source_date_epoch")
    if isinstance(epoch, int) and not isinstance(epoch, bool):
        return str(epoch)

    old = meta.get("built_at", "")
    try:
        ts = str(int(datetime.datetime.fromisoformat(old).timestamp()))
    except (TypeError, ValueError):
        note("    ✗ 这份 meta.json 里没有可用的时间戳"
             "（source_date_epoch / built_at 都没有）")
        note("      —— 拿不到它就没法保证可复现，所以这个包不打了。")
        raise PackError(
            "meta.json 里没有可用的时间戳（source_date_epoch / built_at 都没有）"
            "——拿不到它就没法保证可复现，所以这个包不打了") from None
    note(f"    ⚠ 这份 meta.json 只有旧的 built_at（{old}）"
         f"—— 它的时间戳本来就不确定")
    return ts


# ------------------------------------------------------------------ ELF → depends / provides

def elf_fields(meta: dict, payload: Path, *, scanner=elfdeps.scan
               ) -> tuple[list[str], list[str], str]:
    """扫 ELF，把动态库依赖补进 depends / provides。返回 (depends, provides, 说明行)。

    mkpkg 不扫描 ELF（实测它建出来的包 provides 和 depends 都是空的；Alpine 的
    `so:` 是 abuild + scanelf 生成的）。所以在这之前，动态库依赖一直靠人在
    spec.toml 里手写——而手写就会漏。已经漏过的：

        gawk       NEED libmpfr.so.6 + libgmp.so.10，只声明了 glibc
        apk-tools  NEED libssl.so.3（通过 libapk），得靠人想起来写 openssl

    docs/记录/未声明依赖的审计.md 是【离线的一次性分析】：发现了就手改进配方，
    下次可能又漂。这里把同一件事接进打包流程。

    ★ 两端都要：depends 里的 `so:` 靠 provides 里的 `so:` 解析。只做一端的话，
      装了包会发现"没有谁提供 libz.so.1"。

    ★ 去重：手写的是包名（glibc、openssl），自动的是 soname（so:libc.so.6）。
      两者可以同时存在——apk 会两个都满足，多一个约束不改变结果。

    ★ 扫描实现是 skirt 的一部分（`skirtlib/elfdeps.py`）。它原来叫
      `scripts/elf-deps.py`，被这段代码按【文件路径】硬加载
      （importlib.util.spec_from_file_location），后果是测试 import 不进来。
      现在是普通 import。
    """
    needed, provided = scanner(payload)

    so_deps = [f"so:{n}" for n in needed]
    depends = list(meta.get("depends") or [])
    depends += [x for x in so_deps if x not in depends]

    so_provides = [f"so:{k}={v}" if v else f"so:{k}"
                   for k, v in sorted(provided.items())]
    provides = list(meta.get("provides") or [])
    provides += [x for x in so_provides if x not in provides]

    note = ""
    if len(so_deps) + len(so_provides):
        note = (f"  [ELF] {meta['name']}: +{len(so_deps)} 个 so: 依赖 "
                f"✗ +{len(so_provides)} 个 so: 提供")
    return depends, provides, note


# ------------------------------------------------------------------ mkpkg 的字段

# ★ glibc 自己提供的 soname。它们永远能在任何一个 glibc 系统上找到，
#   所以分发包依赖它们不算风险。
#
#   ★ 硬编码这几个是【故意的】：真正的答案在 glibc 配方的 provides 里，而
#   为了读它去扫一遍 glibc 的 payload 不值得（那要几十秒）。而这份清单很短、
#   很稳定（glibc 不会突然多一个 lib*.so.6），而【如果它真的变了】——
#   下面的报告会多几行，而人一眼能看出是假的。
GLIBC_SONAMES = frozenset({
    "libc.so.6", "libm.so.6", "libdl.so.2", "libpthread.so.0",
    "librt.so.1", "libresolv.so.2", "libutil.so.1", "libanl.so.1",
    "libmvec.so.1", "libnsl.so.1", "libcrypt.so.1",
})


def vendor_risky_needs(meta: dict, payload: Path, declared: list[str],
                       *, scanner=None) -> list[str] | None:
    """对【分发包】报告它链了哪些"我们可能没有"的库（决策记录 0022，5.12.2）。

    ★ 只对 `[build] vendor` 非空的包做 —— 自己建的包依赖由配方声明，
      而声明漏了 `elf_fields` 那头会补；而分发包的依赖【没人声明过】。

    ★ 返回 `None` 表示"这不是分发包，没查"；返回 `[]` 表示"查了，没有可疑的"。
      **这两者必须分开** —— 否则零分发包的时候，这条检查【看起来】通过了，
      而它其实一次都没跑过。那正是这个项目一直在修的那一类。

    ★ 判据是【近似】的，而这是故意的：我们不知道仓库里有哪些 soname
      （那要扫全部包的 payload），所以报的是"链了、而自己没声明"。
      它不替你判断对错，它让【那个人能判断】。

      · glibc 提供的（`GLIBC_SONAMES`）—— 跳过，谁都有的
      · 自己声明了的（`so:xxx` 或包名）—— 跳过，说明作者想过了
      · 剩下的 —— 报出来

    ★ 而它【不失败】。装不上的真正判据是"装上之后跑得起来"，那在 distro
      那边（5.12.3）。这里失败的话，会把一个能用的包挡在门外。
    """
    build = meta.get("build") or {}
    if not build.get("vendor"):
        return None                      # 不是分发包

    if scanner is None:
        scanner = elfdeps.scan
    needed, _ = scanner(payload)
    已声明 = set()
    for d in declared:
        if d.startswith("so:"):
            已声明.add(d[3:])
        else:
            已声明.add(d)                # 包名，不是 soname，但记着无害
    return [n for n in needed
            if n not in GLIBC_SONAMES and n not in 已声明]


def info_fields(job: Job, depends: list[str], provides: list[str]
                ) -> list[tuple[str, str]]:
    """meta.json → `--info k:v` 的那些字段（空值不传：mkpkg 对空字符串会报错）。"""
    m = job.meta
    fields = [
        ("name",        job.name),
        ("version",     job.version),
        ("description", m.get("description", "")),
        ("url",         m.get("url", "")),
        ("license",     m.get("license", "")),
        ("arch",        m.get("arch", "")),
        ("origin",      m.get("built_from", "")),
        ("depends",     " ".join(depends)),
    ]
    # provides / provider-priority：这个包提供什么虚名（比如 /bin/sh），以及
    # 上面扫出来的 so: 名字。
    if provides:
        fields.append(("provides", " ".join(provides)))
    if m.get("provider_priority") is not None:
        fields.append(("provider-priority", str(m["provider_priority"])))
    # ★★ replaces：这个包的文件和别的一个包【撞在一起】时，谁赢。
    #   2026-10-22 加，起因很具体：systemd 也提供 usr/sbin/reboot 和 poweroff，
    #   而 `yojigen-os-init` 也在提供（那两个脚本自己的注释里就写着
    #   "Alpine → busybox，Debian → systemd"）。装 systemd 时 apk 报
    #       ERROR: trying to overwrite usr/sbin/reboot owned by yojigen-os-init
    #   —— 而这两个命令该由 systemd 接管。
    if m.get("replaces"):
        fields.append(("replaces", " ".join(m["replaces"])))
    return [(k, v) for k, v in fields if v]


# ------------------------------------------------------------------ 容器里那一段

# ── 发行版的系统组（GID → 名字）────────────────────────────────────────────
#
# ★★★ 为什么要在驱动里再列一遍（2026-10-23）：
#
#   打包容器里 `chgrp 43` 能成功，但 `mkpkg` 打出来的是 `group: nobody` ——
#   **因为 `mkpkg` 记的是组【名字】**，而它查不到 GID 43 → 退回 nobody。
#
#   所以容器里必须有一行 `utmp:x:43:` 才能记对。而容器是一次性的
#   （`docker run --rm`），写它的 `/etc/group` 无害。
#
# ★★ 而这张表【和 `filesystem` 包的 `files/etc/group` 是同一件事】——
#    两处都写就是为了"驱动能独立跑，不依赖某个包被解开"。
#    `test_pack_groups` 盯着它们不许漂移：**这里改了那边没改，测试会红。**
#
# ★ 值照 Debian 的 base-passwd 抄（那套从 1990 年代沿用至今，是事实标准）。
SYSTEM_GROUPS: tuple[tuple[int, str], ...] = (
    (4, "adm"), (5, "tty"), (6, "disk"), (7, "lp"),
    (15, "kmem"), (20, "dialout"), (24, "cdrom"), (26, "tape"),
    (29, "audio"), (42, "shadow"), (43, "utmp"), (44, "video"),
    (100, "users"),
)


def inner_script(job: Job, depends: list[str], provides: list[str], *,
                 signed: bool) -> str:
    """生成【容器里】跑的 shell（`/pack/inner.sh`）。

    为什么生成脚本而不是拼命令行：描述里有中文、空格、逗号，直接塞进 `sh -c`
    会被吃掉。生成一个文件，引号就由 `shlex.quote` 一处负责。
    """
    fields = info_fields(job, depends, provides)
    sign_arg = f"--sign-key {KEY_IN_CONTAINER} " if signed else ""

    lines = [
        "#!/bin/sh",
        "set -e",
        # ★ getcap/setcap 来自 libcap，而 alpine:latest 里没有它。
        #   装一次（从 Alpine 自己的仓库，很快）比"能力位静默地丢掉"划算。
        #   装不上就继续 —— 下面的 getcap 会失败，而那时 /tmp/.caps 是空的，
        #   等于退回"没有能力位"的旧行为，不会把构建弄挂。
        "apk add --no-cache libcap >/dev/null 2>&1 || true",
        # ★★★ 让容器认得发行版的组名（2026-10-23 加）。
        #
        #   撞到的最后一环：清单里记的是数字 GID（`43 /var/log/wtmp`），
        #   `chgrp 43` 也成功了 —— 而 `mkpkg` 打出来的包是 `group: nobody`。
        #
        #   **因为 `mkpkg` 记的是组【名字】**，它在容器的 `/etc/group` 里查不到
        #   GID 43，就退回 `nobody`（65534）。
        #
        #   ★ 所以让容器认得那个 GID 对应的名字。容器是**一次性的**
        #     （`docker run --rm`），写一行 `/etc/group` 无害，也不影响宿主。
        #
        #   ★ 而这张表就是 SYSTEM_GROUPS —— 它和 `filesystem` 包的
        #     `files/etc/group` 是同一件事，由 `test_pack_groups` 盯着不许漂移。
        f"for e in {' '.join(f'{g}:{n}' for g, n in SYSTEM_GROUPS)}; do",
        '  gid=${e%%:*}; gname=${e##*:}'
        '  grep -q "^$gname:" /etc/group 2>/dev/null || printf "%s:x:%s:\\n" "$gname" "$gid" >> /etc/group'
        'done',

        # ★ rsync 也要装 —— 理由同 build.py：cp 不保 security.* 扩展属性，
        #   而能力位在那里。apk 装一个 rsync 换来能力位不丢，是划得来的。
        "apk add --no-cache rsync >/dev/null 2>&1 || true",
        "mkdir -p /data",
        # ★ 有 rsync 就用它 —— cp【不保 security.* 扩展属性】，而能力位在那里。
        #   没有就退回 cp -a（那时能力位会丢，但属主和权限还是对的）。
        #   **而"用了哪个"要说出来**，不静默。
        "if command -v rsync >/dev/null 2>&1; then "
        "rsync -aX /in/payload/. /data/ && echo '[打包] 用 rsync -aX（保能力位）'; "
        "else cp -a /in/payload/. /data/ && echo '[打包] ⚠ 没有 rsync，"
        "退回 cp -a —— 能力位会丢'; fi",
        # ── 属主统一成 root:0，而 setuid/setgid 位要保住 ────────────────────
        #
        # 为什么需要这一步：payload 是从容器里【拷到宿主】再挂回来的，而沿途有
        # 一次清理用的 `chown -R "$HOSTUID"`（见 build.py 的 cleanup）。于是包里
        # 的属主成了构建用户（1001），而不是 root。
        #
        # 实测的两头：
        #     dest 里（package 之后，诊断打的）  -rwsr-xr-x root  root  sudo ✓
        #     发出去的包里（解开发出来的）      -rwxr-xr-x nobody nobody ✗
        #
        # 而"属主不是 root"这件事的后果我们撞过两次：
        #     · sudo      没有 setuid 就不叫 sudo
        #     · .ssh      StrictModes 拒绝非 root 拥有的 authorized_keys
        #                 → 在运行中的系统上升级 openssh 之后 SSH 直接进不去
        #
        # 所以【发行版的包，文件就该是 root:0】。这是个决定，不是默认值 ——
        # 而上游的 Makefile 本来也是这么装的（install -o 0 -g 0）。
        #
        # ★ 顺序：先记下带 setuid/setgid 的那些（连同它们的完整权限），再 chown，
        #   再把权限放回去 —— 因为 Linux 的 chown 会清掉那两位。
        "find /data \\( -perm -4000 -o -perm -2000 \\) -type f 2>/dev/null"
        " | while IFS= read -r f; do echo \"$(stat -c %a \"$f\") $f\"; done"
        " > /tmp/.setid || true",
        # ★ 能力位也要记 —— 它的道理和 setuid 完全一样：chown 会清掉
        #   security.capability。（iputils 给 ping 设的 cap_net_raw 就是这么丢的。）
        # ★ 显式找工具（`command -v` 之后退回 /usr/sbin），并且【明说】你有没有
        #   真的记到东西 —— 上一版写了裸的 getcap + `|| true`，于是它静默地
        #   什么都没做，能力位照样丢。
        "GC=$(command -v getcap 2>/dev/null || true);"
        " [ -x \"$GC\" ] || GC=/usr/sbin/getcap",
        "if [ -x \"$GC\" ]; then \"$GC\" -r /data > /tmp/.caps 2>/dev/null || true; fi",
        "echo \"[打包] 记下 $(wc -l < /tmp/.caps 2>/dev/null || echo 0) 个能力位\"",
        # ★★ 而【非 root 的组属主】要保住 —— 2026-10-23 加。
        #
        #   上面那个 `chown -R 0:0` 修的是"属主变成构建用户"，但它顺手把
        #   **组**也拍平了。而有些文件的组属主是【有意义的】：
        #
        #       /var/log/wtmp   root:utmp     who / last 靠它写记录
        #       /var/log/btmp   root:utmp     lastb 靠它
        #       /usr/bin/wall   root:tty      往终端广播
        #
        #   ★ 而 apk 的记录是【按名字】的（`adbdump` 里是 `group: root`），
        #     不是数字 GID —— 所以 `root:utmp` 能原样进包，装的时候 apk 按
        #     目标系统的 /etc/group 解析。**这正是 Debian 的 dpkg 的做法**。
        #
        #   ★★ 所以顺序是：先记下非 root 的组（**按名字**）、chown 拍平、
        #      再把那些组按名字设回去。和上面 setuid / 能力位那两段是同一个
        #      形状 —— 那两样也是"chown 会毁掉、所以先记后补"。
        #
        #   为什么按名字而不是数字：构建容器里的 GID 和目标系统上的可能不同
        #   （`utmp` 在容器里是 101、在目标上是 43 也说不定）。记名字才对得上。
        # ★★ 组属主从【清单】读，不是现场扫（2026-10-23 修正）。
        #
        #   现场扫不出来的原因：`build.py` 的 `cleanup()` 里有一句
        #       chown -R "$HOSTUID:$HOSTGID" /in
        #   —— payload 从容器出来时，**所有文件的组已经被改成构建用户的组**。
        #   于是这里 `find ! -group 0` 一个都找不到（实测：tar 里读出来是 `0 0`）。
        #
        #   ★ 所以清单由 `build.py` 在【改之前】写（`.yojigen-groups`），
        #     和 `.yojigen-caps` 完全同一个理由：属性过不了那条路，文本可以。
        "GROUPS=/data/.yojigen-groups",
        "if [ -s \"$GROUPS\" ]; then"
        "  cp \"$GROUPS\" /tmp/.groups;"
        "  echo \"[打包] 从清单读到 $(wc -l < /tmp/.groups) 个非 root 的组属主\" >&2;"
        # ★ 而清单【内容】也打出来（前 3 条）—— 只看条数分不清"记对了没有"。
        #   这条是 2026-10-23 加的：当时清单有 18 条、chgrp 也成功，
        #   而 .apk 里还是 root:root，光看条数完全不知道差在哪。
        "  head -3 /tmp/.groups | sed \"s|^|    · |\" >&2 || true;"
        "else"
        "  : > /tmp/.groups;"
        "  echo \"[打包] 没有 .yojigen-groups —— 这一批的组属主都是 root\";"
        "fi",
        "chown -R 0:0 /data",
        # ★ 补回组属主。用 `chgrp` 按名字 —— target 系统上没有那个组时它失败，
        #   而失败【要说出来】：一个静默没补上的组，症状是"who 不记录登录"，
        #   离成因很远。
        "GRPFAIL=0",
        "while IFS=' ' read -r g f; do",
        "  [ -n \"$g\" ] && [ -n \"$f\" ] || continue",
        # ★★★ 路径要加 `/data` 前缀（2026-10-23 修）。
        #
        #   清单里的路径是 `/var/log/wtmp`（相对 payload 根），而 files 在
        #   容器里落在 `/data/var/log/wtmp`。原来这一行少了 `/data`，于是
        #   `chgrp 43 /var/log/wtmp` 作用在**容器的根上** —— 那里没有这个
        #   文件，全部失败。实测：18 条清单里 17 条补不回。
        #
        #   ★ 而同一个文件里，能力位那行是 `setcap \"$c\" \"/data$f\"` ——
        #     **有**前缀。两行干的是同一类事（把清单里的路径映射到文件），
        #     而一行对一行错。这种"隔壁那行是对的"最容易看漏。
        "  chgrp \"$g\" \"/data$f\" 2>/dev/null || { GRPFAIL=$((GRPFAIL+1)); echo \"  ⚠ 补不回 /data$f 的组 $g\"; }",
        "done < /tmp/.groups || true",
        "[ \"$GRPFAIL\" -gt 0 ] && echo \"[打包] ⚠ $GRPFAIL 个文件的组属主没补回（构建容器里没有那个组）\" || true",
        "while IFS=' ' read -r m f; do chmod \"$m\" \"$f\" 2>/dev/null || true; done"
        " < /tmp/.setid || true",
        "SC=$(command -v setcap 2>/dev/null || true);"
        " [ -x \"$SC\" ] || SC=/usr/sbin/setcap",
        "while read -r f c; do [ -n \"$c\" ] && \"$SC\" \"$c\" \"$f\" 2>/dev/null || true;"
        " done < /tmp/.caps",
        "echo \"[打包] 从 getcap 记到的放回了"
        " $(wc -l < /tmp/.caps 2>/dev/null || echo 0) 个\"",
        # ── 而【主路】是那份文本清单 ──────────────────────────────────────
        #
        # 它由 build.py 写在 payload 里（`.yojigen-caps`）。理由：能力位过不了
        # 绑定挂载（fsetxattr 报 EOPNOTSUPP），而文本可以。所以走清单不走属性。
        #
        # ★ 而它必须在这里设 —— 在 chown 之后。因为 chown 会清掉能力位。
        "CAPS=/data/.yojigen-caps",
        "if [ -s \"$CAPS\" ] && [ -x \"$SC\" ]; then",
        "  n=0; bad=0",
        "  while IFS=' ' read -r f c; do",
        "    [ -n \"$f\" ] && [ -n \"$c\" ] || continue",
        "    if \"$SC\" \"$c\" \"/data$f\" 2>/dev/null; then n=$((n+1)); else bad=$((bad+1)); fi",
        "  done < \"$CAPS\"",
        "  echo \"[打包] ★ 设回了 $n 个能力位$([ \"$bad\" -gt 0 ] && echo \"，$bad 个失败\")\"",
        "else",
        "  echo \"[打包] 没有 .yojigen-caps（或没有 setcap）—— 这一批不需要设能力位\"",
        "fi",
        # ★ 而清单本身要删掉 —— 它是构建过程的记事，不是这个包的内容。
        "rm -f \"$CAPS\"",
        # ★ 而组清单同样要删 —— 同理，它是构建过程的记事。
        "rm -f \"$GROUPS\"",
    ]

    # ── mkpkg ────────────────────────────────────────────────────────────
    #
    # ★ 签名必须在这里做——包建出来就是签好的。
    #   不然后面 mkndx 建索引时会逐个验证，全部报 UNTRUSTED signature，
    #   然后拒绝建索引（"14 errors, not creating index"）。
    lines.append(f"exec apk mkpkg {sign_arg}\\")
    for k, v in fields:
        lines.append(f"  --info {shlex.quote(f'{k}:{v}')} \\")
    # ── 安装脚本 ──────────────────────────────────────────────────────────
    #
    # 产物目录里的 scripts/<名字>，名字就是 mkpkg 的脚本类型（post-install、
    # post-upgrade…）。collect.py 把它们从配方的 scripts/ 收过来。
    #
    # ★ 路径写成 /in/scripts/<名字> —— /in 就是那个产物目录（挂载见 host_argv）。
    #
    # 为什么需要这条通路：见 collect.py 里那段注释。一句话——openssh 的
    # /root/.ssh/authorized_keys 从"归包"改成"不归包"之后，升级路径上会被 apk
    # 删掉，需要 post-install 立刻补回来。
    if job.scripts.is_dir():
        for f in sorted(job.scripts.iterdir()):
            if f.is_file():
                lines.append(f"  --script "
                             f"{shlex.quote(f.name + ':/in/scripts/' + f.name)} \\")
    lines.append(f"  --files /data --output {shlex.quote('/out/' + job.apk)}")
    return "\n".join(lines) + "\n"


def host_argv(job: Job, *, pkgs_dir: Path, packdir: Path, epoch: str,
              sign_key: Path | None, image: str = PACK_IMAGE) -> list[str]:
    """宿主上跑的那条 `docker run`（生成 argv，不执行 —— 所以测试能钉住它）。

    ★ `-v ...:/in:ro` 挂的是【产物目录】：inner.sh 从 /in/payload 取 payload、
      从 /in/scripts 取安装脚本。`/out` 是打好的 .apk 落点（宿主上的 var/pkgs）。
    """
    argv = ["docker", "run", "--rm",
            "-e", f"SOURCE_DATE_EPOCH={epoch}",
            "-v", f"{job.product}:/in:ro",
            "-v", f"{pkgs_dir}:/out",
            "-v", f"{packdir}:/pack:ro"]
    if sign_key is not None:
        argv += ["-v", f"{sign_key}:{KEY_IN_CONTAINER}:ro"]
    return argv + [image, "sh", "/pack/inner.sh"]


# ------------------------------------------------------------------ 打一个包

def pack_one(job: Job, *, pkgs_dir: Path, work: Path, sign_key: Path | None,
             image: str = PACK_IMAGE, runner=subprocess.run, note=print) -> bool:
    """打一个包。返回是否成功（**判据是产物，不是退出码**）。"""
    packdir = pack_dir(work, job.product)
    packdir.mkdir(parents=True, exist_ok=True)

    depends, provides, elf_note = elf_fields(job.meta, job.payload)
    if elf_note:
        print(elf_note, file=sys.stderr)

    # ★ 5.12.2：分发包链了哪些"我们可能没有"的库。**只报告，不失败**
    #   （理由见 vendor_risky_needs 的 docstring）。
    #
    #   ★ 而 `None` 和 `[]` 的区别在这里很重要：前者是"自己建的，没查"，
    #   后者是"分发包，查了，干净"。两种都要说话，否则零分发包时这条检查
    #   会看起来一直在工作。
    风险 = vendor_risky_needs(job.meta, job.payload, depends)
    if 风险 is None:
        pass                          # 自己建的包，这格不适用（也不打印，免得刷屏）
    elif 风险:
        名 = job.meta["package"]["name"]
        vendor = (job.meta.get("build") or {}).get("vendor")
        print(f"[分发包] ⚠ {名} 来自上游二进制（{vendor}）：", file=sys.stderr)
        for so in 风险:
            print(f"            {so}", file=sys.stderr)
        print("          ★ 这些它链了、而配方没声明 —— 它们可能不在我们的仓库里，"
              "于是装上之后跑不起来。", file=sys.stderr)
        print("            要么把提供它的包加进 depends，要么记进 0022 的验收。",
              file=sys.stderr)
    else:
        名 = job.meta["package"]["name"]
        print(f"[分发包] {名} 的库依赖都声明过了（或由 glibc 提供）", file=sys.stderr)

    inner = packdir / "inner.sh"
    inner.write_text(inner_script(job, depends, provides, signed=sign_key is not None),
                     encoding="utf-8")
    inner.chmod(0o755)

    out = pkgs_dir / job.apk
    out.unlink(missing_ok=True)      # 先删旧的，否则"产出检查"可能是看到上一次的
    log_file = work / "log"

    note(f"  {job.apk:<30} ", end="")
    argv = host_argv(job, pkgs_dir=pkgs_dir, packdir=packdir, epoch=job.epoch,
                     sign_key=sign_key, image=image)
    try:
        proc = runner(argv, capture_output=True, text=True)
        log = (proc.stdout or "") + (proc.stderr or "")
        code = proc.returncode
    except FileNotFoundError:
        die("没有 docker —— 打包要靠容器里的 apk mkpkg（宿主上没有 apk）")
        raise AssertionError("unreachable")

    log_file.write_text(log, encoding="utf-8")

    # ★ 两道检查，缺一不可：
    #   退出码是 0 不等于【产出了东西】。上面那次就是这样——docker 成功了，
    #   文件根本没生成，而脚本报了 ✓。让错误传上来还不够，还要检查结果。
    if out.is_file() and out.stat().st_size > 0:
        note(f"✓ {out.stat().st_size} 字节")
        _sync_sidecar(job, pkgs_dir, note)
        # ★★★ 成功时也要把【关键诊断】打出来（2026-10-23 加）。
        #
        #   撞到的：为了查"组属主为什么丢了"，我在 inner.sh 里加了几行
        #   `[打包] 从清单读到 N 条` —— 而它们在 CI 日志里**一行都不出现**。
        #
        #   原因就是上面那句 `return True`：`inner.sh` 的输出被
        #   `capture_output` 收进 `<产物>/log`，而那个文件**只在失败时才被
        #   打出来**。成功路径上它是黑盒。
        #
        #   ★ 而"成功"这件事恰恰不等于"做对了"—— 组属主就是这样：打包
        #     成功、字节数正常、`.apk` 里的组却是 root。**成功路径上的诊断
        #     比失败路径上的更重要**，因为失败至少还有退出码。
        #
        #   ★ 只挑和"属性有没有带到"有关的行，不是把整个 log 倒出来
        #     （那会把 CI 日志淹掉，而淹掉之后就没人看了）。
        for line in log.splitlines():
            if any(k in line for k in ("清单", "组属主", "能力位", "补不回")):
                note(f"      {line.strip()}")
        return True

    note("✗")
    # 日志可能是空的。空日志本身是信息——说明 docker 都没跑起来，那种情况要看
    # 退出码和挂载，而不是看 mkpkg 的报错。
    if log.strip():
        for line in log.splitlines()[:8]:
            note(f"      {line}")
    else:
        note("      （docker 没有任何输出——它可能没跑起来）")
        note(f"      退出码: {code}   挂载: {job.product} → /in，"
             f"{packdir} → /pack，{pkgs_dir} → /out")
    return False


def _sync_sidecar(job: Job, pkgs_dir: Path, note=print) -> None:
    """把 `.buildkey` 旁路文件搬到 .apk 旁边（或者把对不上的旧那份删掉）。

    为什么它要跟着 .apk 走：CI 保存/恢复的是 var/pkgs 里那批 .apk（见
    release.yml 的缓存），meta.json 不跟着走。没有这份，跳过判断就只能说
    "我不知道它上次是什么样"—— 于是每次都重建，build_key 那个机制就白做了。
    """
    src = job.product / ".buildkey"
    dest = pkgs_dir / sidecar_name(job.apk)
    if src.is_file():
        shutil.copyfile(src, dest)
    else:
        # 没有旁路文件 = 这个包是 build_key 出现【之前】建的。
        # **说出来，别假装有** —— 而顺手删掉旧的，免得留下一份对不上的。
        note("      ⚠ 没有 .buildkey（这包是 build_key 之前建的）"
             "—— 跳过判断会当它未知")
        dest.unlink(missing_ok=True)


# ------------------------------------------------------------------ 总入口

def pack(arch: str, names: list[str] | None = None, *, all_: bool = False,
         sign_key: Path | None = None,
         pkgs_dir: Path | None = None, output_dir: Path | None = None,
         runner=subprocess.run, note=print) -> tuple[int, int, int]:
    """打一批包。返回 (成功, 失败, 跳过) 三个数。

    ★ 拿不到时间戳那种错是**抛出去**的（`PackError`），不是记一个失败继续打
      下一个 —— 因为那说明 meta.json 这一代有问题，继续打只会再撞一次。这和
      shell 版的行为一致：那边 `set -e` 会在同一个地方把整个脚本停下来。
    """
    pkgs_dir = PKGS_DIR / arch if pkgs_dir is None else pkgs_dir
    output_dir = OUTPUT_DIR if output_dir is None else output_dir

    if not all_ and not names:
        raise PackError("用法: skirt pack <包名>… 或 --all")

    if sign_key is not None and not sign_key.is_file():
        raise PackError(f"找不到签名密钥: {sign_key}")

    products = select_products(arch, None if all_ else names, output_dir=output_dir)
    if not products:
        raise PackError("找不到产物。先 skirt build 建它。")

    pkgs_dir.mkdir(parents=True, exist_ok=True)
    if sign_key is not None:
        note(f"  签名: {sign_key.name}")

    ok = fail = skip = 0
    with tempfile.TemporaryDirectory(prefix="skirt-pack-") as td:
        work = Path(td)
        for product in products:
            job = plan(product)
            if job is None:
                note(f"  跳过 {product.name}（没有 meta.json）")
                skip += 1
                continue
            job.epoch = epoch_of(job.meta, note=note)
            if pack_one(job, pkgs_dir=pkgs_dir, work=work, sign_key=sign_key,
                        runner=runner, note=note):
                ok += 1
            else:
                fail += 1

    note("")
    summary = f"  打了 {ok} 个"
    if fail:
        summary += f"，失败 {fail} 个"
    if skip:
        summary += f"，跳过 {skip} 个"
    note(summary)
    note(f"  输出: {_shown(pkgs_dir)}")
    note("")
    return ok, fail, skip
