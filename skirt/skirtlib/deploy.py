"""把 var/repo/<架构>/ 的内容传到线上（默认干跑）。

    skirt deploy rustfs:yojigen-os-repo            # 干跑，只看会传什么
    skirt deploy rustfs:yojigen-os-repo --go       # 真的传
    skirt deploy --go --arch aarch64               # 远端从环境变量拼（yojigen:<桶>）

目标是一个 rclone remote 加桶路径。S3 兼容的服务都行——RustFS、R2、S3、MinIO。

── 部署就一件事：把 var/repo/ 的内容弄上去 ─────────────────────────────

    repo.os.yojigen.cn/            ← 桶的根 = 这个域名
    └── aarch64/
        ├── APKINDEX.tar.gz        ← apk 第一个拿的就是它
        └── …（每个包每个版本一份）

  ★ 客户端写 `--repository https://repo.os.yojigen.cn`
    apk 自己拼 https://repo.os.yojigen.cn/aarch64/APKINDEX.tar.gz

── 两个必须注意的地方 ────────────────────────────────────────────────────

**① 这个命令只做"加"，不做"减"。**

所以用 `rclone copy`，**绝不能**用 `rclone sync`——后者会把目标端多出来的文件
删掉，而那是另一个动作、该由另一个地方决定。"删"现在是 `skirt prune`（决策记录
`docs/决策记录/0020-软件包的清理策略.md`）。

★ 这一节原来写的是「**绝不删旧版本**」，理由写的是「manifest 钉住版本，半年后
  还要能按同样的版本重建」。**那个理由 2026-10-15 被推翻了** —— manifest 是
  【记录】不是【承诺】：它记录这台机器当时从哪些包建出来，而不承诺将来还能把你
  装的那一组具体 `-rX` 重新拼出来。真正要能重建的是【某个提交上的配方】，那件
  事由 build_key 那套保证。

  ★ 而"用 copy 不用 sync"这条机制**没有变**：变的是它的理由，以及"减"现在有个
    地方可去（在那之前它是"谁也不许删"）。

**② 先传包，后传索引。**

apk 是先拿 APKINDEX、再按索引去拿包的。如果索引先到、而它列的包还没传完，
客户端就会 404。反过来则永远是"索引列出的包都已经在"的状态。

所以分两次：先 copy 除索引外的一切，最后单独 copy 索引。**索引是【切换点】。**

★ 于是这里有一条不显眼但重要的性质：**第一步失败时第二步不跑**（见 `deploy()`）
  —— 那正是"索引列了而包没到"那个状态的对立面。

── rclone 用哪一个 ──────────────────────────────────────────────────────

有本机 rclone 就用本机的（快一点），没有就起容器（`rclone/rclone`）。这条和
tar 那次同一个理由：只有一个实现，就不存在"两个版本行为不同"。

★ 用容器时源路径是容器里的挂载点（`/repo/<架构>`），所以路径要跟着挂载走 ——
  这正是 `配方约定.md`「两个同名但不同的路径」那一节的事。

── 凭据：环境变量优先，省掉交互式 rclone config ──────────────────────────

本地和 CI 用【同一套】写法。CI 里那几个 organization secret 直接映射过来：

    S3_AK        → RCLONE_CONFIG_YOJIGEN_ACCESS_KEY_ID
    S3_SK        → RCLONE_CONFIG_YOJIGEN_SECRET_ACCESS_KEY
    S3_BASE_URL  → RCLONE_CONFIG_YOJIGEN_ENDPOINT
    S3_BUCKET    → 桶名（不是 remote 配置，是路径的一部分）

★ 用【具名 remote】（`yojigen:`）而不是 `:s3:` 连接字符串 —— 只有具名 remote
  才读那些 `RCLONE_CONFIG_*` 环境变量。用 `:s3:` 的话它会报

      s3 provider "" not known

  然后拿默认值去连（而那个错看起来像"凭据不对"）。这一段是踩出来的。

── 传大包要强制分片 ─────────────────────────────────────────────────────

    --s3-upload-cutoff 8Mi

rclone 的 `--s3-upload-cutoff` 默认是 200Mi —— **小于它的文件一次 PUT 传完**。
而前面那层 nginx 对【单个请求】的 body 有上限，于是大的包被挡：

    ERROR : gcc-16.2.0-r1.apk:     StatusCode: 413   (95.3 MiB)
    ERROR : python3-3.14.8-r1.apk: StatusCode: 413   (61.6 MiB)
    47 / 49 传完了，就这两个大的被挡

（413 = Payload Too Large。后面还跟着一句「XML syntax error: element <hr>
closed by </body>」——那个"XML"其实是 nginx 的 HTML 错误页。）

S3 的分片上传把大文件拆成多个 HTTP 请求，每个分片是独立的 PUT。所以只要把
cutoff 调到比那两个文件小，限制就绕过去了——**不用改服务端**。

8Mi 这个值：8Mi 以上的文件都走分片，而每片是 `--s3-chunk-size 5Mi`，远低于任何
合理的请求体上限。小文件仍然一次传完（少几次往返）。

★ 为什么不是"把包做小"：gcc 95MiB 是编译器本体，已经 strip 过了。为了绕一个
  上传限制去拆包是本末倒置——而且拆完还是要传同样多的字节。

★ aarch64 从来没撞上，因为它的仓库里没有 gcc。这是发布一个 95MiB 的包才暴露的。

── 它是从 shell 搬过来的（2026-10-15）────────────────────────────────────

原来是 `scripts/deploy.sh`（219 行）。判定（传什么、怎么拼 argv、干跑还是真传、
顺序）现在都在 Python 里，`skirt/tests/test_deploy.py` 守着它们 —— 之前那些
"只能靠读一遍脚本才知道"的东西（比如"第一步失败第二步就不跑"）现在有测试。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from .common import REPO_DIR, ROOT, die
from .repo import (INDEX_NAME, REMOTE_NAME, mark_signed_path, rclone)

#: 容器里的 rclone。本机装了 rclone 就不用它。
RCLONE_IMAGE = "rclone/rclone"

#: 拼 rclone 配置要有这三样。**不要拿 `repo.has_s3_credentials()` 当判据** ——
#: 那个查的是另一组（S3_BASE_URL / S3_BUCKET / S3_AK，它回答的是"能不能列线上"），
#: 少了 S3_SK 它照样说"有"。而少了 SK，rclone 配置是拼不出来的：
#:
#:     KeyError: 'S3_SK'
#:
#: ★ 这就是"两个函数各说各话"的形状 —— 判据不同就该分开写，别借。
RCLONE_VARS = ("S3_AK", "S3_SK", "S3_BASE_URL")

#: 远端那边的路径：`/repo/<架构>`（容器里的挂载点）。
REPO_IN_CONTAINER = "/repo"


# ------------------------------------------------------------------ 凭据

def rclone_env(env: dict | None = None) -> dict[str, str]:
    """从 `S3_*` 拼出 rclone 读的那几个环境变量。

    ★ 值一样是【具名 remote】（`yojigen:`）那一套 —— 见模块顶上那段：
      用 `:s3:` 连接字符串不读这些变量。
    """
    env = os.environ if env is None else env
    if [k for k in RCLONE_VARS if not env.get(k)]:
        return {}
    return {
        "RCLONE_CONFIG_YOJIGEN_TYPE": "s3",
        "RCLONE_CONFIG_YOJIGEN_PROVIDER": "Other",
        "RCLONE_CONFIG_YOJIGEN_ENDPOINT": env["S3_BASE_URL"],
        "RCLONE_CONFIG_YOJIGEN_ACCESS_KEY_ID": env["S3_AK"],
        "RCLONE_CONFIG_YOJIGEN_SECRET_ACCESS_KEY": env["S3_SK"],
        # ★ RustFS / MinIO 都是路径式。不开这个的话 rclone 会用虚拟主机式
        #   （bucket.host 形式），而那个通常没配 DNS。
        "RCLONE_CONFIG_YOJIGEN_FORCE_PATH_STYLE": "true",
        # 自建服务没有 region 概念，但 SDK 要一个值
        "RCLONE_CONFIG_YOJIGEN_REGION": "us-east-1",
    }


def resolve_remote(remote: str | None, env: dict | None = None) -> str | None:
    """远端在哪儿：命令行给的优先，没给就用环境变量拼出来的那个。

    `yojigen:<S3_BUCKET>` —— 桶名不是 remote 配置，是路径的一部分。
    """
    env = os.environ if env is None else env
    if remote:
        return remote
    if env.get("S3_BUCKET"):
        return f"{REMOTE_NAME}:{env['S3_BUCKET']}"
    return None


# ------------------------------------------------------------------ 要传什么

def source_dir(arch: str, repo_dir: Path | None = None) -> Path:
    return (REPO_DIR if repo_dir is None else repo_dir) / arch


def local_rclone() -> bool:
    """这台机器上有没有 rclone（有就用本机的，快一点）。"""
    return shutil.which("rclone") is not None


#: 传包那一步的参数。值都有理由，见模块顶上"传大包要强制分片"那段。
PACKAGES_ARGS = ("--checksum", "--exclude", INDEX_NAME,
                 "--s3-upload-cutoff", "8Mi", "--s3-chunk-size", "5Mi",
                 "--s3-upload-concurrency", "4", "--progress")


def steps(arch: str, remote: str, *, go: bool, src: str | None = None
          ) -> list[tuple[str, list[str]]]:
    """两步 rclone 的 argv（**生成，不执行** —— 所以测试能逐项钉住）。

    返回 [(说明, argv), …]，顺序就是执行的顺序：

        ① 传包（除索引外的一切）
        ② 传索引（切换点）

    ★ `src` 是【rclone 看到的】源目录。用本机 rclone 时它是个宿主路径；用容器
      时它是 `/repo/<架构>`（挂载点）。所以"路径"这件事只在这里拼一次。
    """
    src = src if src is not None else str(source_dir(arch)) + "/"
    dest = f"{remote}/{arch}/"
    dry = [] if go else ["--dry-run"]
    return [
        ("传包（不含索引）",
         ["copy", src, dest, *PACKAGES_ARGS, *dry]),
        ("传索引（这一步之后新版本才对外可见）",
         ["copy", f"{src.rstrip('/')}/{INDEX_NAME}", dest, "--checksum",
          "--progress", *dry]),
    ]


def _run(args: list[str], *, env: dict, mounts: list[tuple],
         image: str = RCLONE_IMAGE, local: bool) -> subprocess.CompletedProcess:
    """跑一次 rclone —— 本机那份，或者容器里那份。"""
    if local:
        return subprocess.run(["rclone", *args], capture_output=True, text=True,
                              env={**os.environ, **rclone_env(env)})
    return rclone(args, env, mounts=mounts, image=image)


# ------------------------------------------------------------------ 总入口

def deploy(arch: str, remote: str | None = None, *, go: bool = False,
           repo_dir: Path | None = None, env: dict | None = None,
           mounts: list[tuple] | None = None, image: str = RCLONE_IMAGE,
           local: bool | None = None, runner=_run, out=print) -> int:
    """把 var/repo/<架构>/ 传上去。返回退出码（0 = 好）。

    ★ 干跑是默认（`--go` 才真传）—— 这一条从 shell 版原样留着：发布这件事
      不该只差一个回车。

    ★ 第一步失败就**停**，不传索引。"索引列了、包没到"是最坏的那个状态
      （客户端 404），而它恰好是"先传索引"或"传一半也接着传索引"的产物。
    """
    env = os.environ if env is None else env
    src_dir = source_dir(arch, repo_dir)
    index = src_dir / INDEX_NAME

    if not index.is_file():
        die(f"找不到 {index}。先跑 ./skirt/skirt repo。")

    remote = resolve_remote(remote, env)
    if not remote:
        die("用法: skirt deploy <rclone remote>:<桶路径> [--go]\n"
            "       也可以把 S3_AK / S3_SK / S3_BASE_URL / S3_BUCKET 放进环境变量，"
            "那样连远端都不用写")

    apks = sorted(p for p in src_dir.glob("*.apk") if p.is_file())
    size = sum(p.stat().st_size for p in apks) + index.stat().st_size
    creds = rclone_env(env)

    use_local = local_rclone() if local is None else local
    if use_local:
        src_for_rclone = str(src_dir) + "/"
        mounts = [] if mounts is None else mounts
    else:
        src_for_rclone = f"{REPO_IN_CONTAINER}/{arch}"
        mounts = list(mounts or []) + [(src_dir.parent, REPO_IN_CONTAINER)]

    out("")
    out(f"  源:      {src_dir}   （{len(apks)} 个包 + 索引，{_human(size)}）")
    out(f"  目标:    {remote}/{arch}/")
    out(f"  模式:    {'★ 真的传' if go else '干跑（加 --go 才真的传）'}"
        f"   rclone: {'本机那份' if use_local else f'容器里的 {image}'}")
    if creds:
        out(f"  凭据:    来自环境变量（S3_BASE_URL={env.get('S3_BASE_URL')}）")
    elif not use_local:
        out("  凭据:    ★ 没有 S3_* 环境变量 —— 容器里那份 rclone 会拿默认值去连")

    # ── 签名状态 ─────────────────────────────────────────────────────────
    #
    # ★ 签名是打在 APKINDEX.tar.gz【里面】的一个成员，不是仓库目录里的独立文件。
    #   所以这里不去读索引自己判断（索引是 ADB 格式，`tar -tzf` 什么都解不出来，
    #   而里面的签名标记没有可 grep 的原始字节）—— 让【签名的那一方】留记号：
    #   `skirt repo` 签完写 var/.repo-signed-<arch>（见 repo.mark_signed）。
    marker = mark_signed_path(ROOT, arch)
    if marker.is_file():
        out(f"  签名:    有（客户端不用加 --allow-untrusted）")
        out(f"           密钥: {marker.read_text(encoding='utf-8').strip()}")
    else:
        out("  签名:    ★ 没有 —— 客户端装的时候要加 --allow-untrusted")
        out("           生成密钥后跑 ./skirt/skirt repo --sign-key <密钥> 再部署")
    out("")

    failures = 0
    for i, (title, args) in enumerate(
            steps(arch, remote, go=go, src=src_for_rclone), 1):
        out(f"  [{i}/2] {title}")
        proc = runner(args, env=env, mounts=mounts, image=image, local=use_local)
        text = (proc.stdout or "") + (proc.stderr or "")
        if text.strip():
            for line in text.splitlines():
                out(f"    {line}")
        if proc.returncode != 0:
            failures += 1
            out(f"    ✗ rclone 退出码 {proc.returncode}")
            # ★ 停在这里不传索引：见 docstring。
            out("    索引【没有】传 —— 先把这个修好（索引先到会有客户端 404）")
            break

    out("")
    if failures:
        return 1
    if go:
        out("  ✓ 部署完成")
        out("")
        out("  验证（要等反代和桶策略配好）：")
        out(f"      curl -sSI https://repo.os.yojigen.cn/{arch}/{INDEX_NAME}")
        out("      apk add --allow-untrusted --repository https://repo.os.yojigen.cn \\")
        out("              --root /tmp/r --initdb hello")
    else:
        out("  这是干跑。加 --go 才真的传。")
    out("")
    out("  ★ 旧版本【不会被删】——用的是 copy 不是 sync")
    out("     （「减」是另一个动作，由 skirt prune 管 —— 决策记录 0020）")
    out("")
    return 0


def _human(n: int) -> str:
    """字节数 → 人能读的写法。

    ★ 这里算的是【文件大小之和】，不是 `du` 那个"占了多少块"。原来那行是
      `du -sh`，它按块算（还带文件系统的分配粒度）—— 而"要传多少字节"才是
      这里该说的话。
    """
    value = float(n)
    for unit in ("B", "KiB", "MiB"):
        if value < 1024:
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GiB"
