"""源码获取：下载、sha256 校验、解压。"""

from __future__ import annotations

import tarfile
import urllib.request
from pathlib import Path

from .common import CACHE_DIR, die, log, rmtree_force, sha256_of

from .spec import expand

# ------------------------------------------------------------------ git 形式

def _git_candidates(src: dict) -> list[str]:
    """git 形式里的仓库地址，主仓库在前，然后依次是镜像。"""
    out = [src["git"].rstrip("/")]
    for m in src.get("mirrors", []):
        m = m.rstrip("/")
        if m not in out:
            out.append(m)
    return out


def _repo_name(git_url: str) -> str:
    """从仓库地址里取个短名字，用来给缓存文件命名。"""
    return git_url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")


def fetch_git(spec: dict, src: dict) -> Path:
    """从 git 仓库取某个 commit，做成 tar.gz 放进缓存。

    【为什么用 git 而不是托管商的归档链接】

    配方该说的是「哪个仓库的哪个 commit」，不是「去哪台机器的哪个路径拿」。
    链接格式是托管商定的，而且各不相同：GitHub 是 codeload，Gitea/Forgejo
    是 /archive/<sha>.tar.gz，GitLab 又是另一种。写配方的人不该记这些。

    更重要的是校验：**git 自己校验 commit**。commit hash 就是那棵树的内容
    哈希，比 sha256 更强——sha256 钉的是一份【字节】，而不同托管生成的归档
    字节可以不一样，于是单个 sha256 根本盖不住多个镜像。

    所以 git 形式【不需要 sha256】，也就天然支持任意数量的镜像：主仓库不通
    就换下一个，内容对不对由 git 保证。

    【实现】clone 到临时目录，再用 `git archive` 打一份 tar.gz。
    git archive 的输出是确定性的，配合 SOURCE_DATE_EPOCH 满足 0005。
    """
    pkg = spec["package"]
    commit = src.get("commit")
    if not commit:
        die(f"{pkg['name']}: [[source]] 用了 git 但没写 commit")

    repos = _git_candidates(src)
    # 缓存名里带 commit：换了 commit 自然就是另一个文件，不会用到旧的。
    # （这正好避开「改了源但缓存没失效」那个坑。）
    dest = CACHE_DIR / f"{_repo_name(repos[0])}-{commit[:12]}.tar"
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    if dest.is_file():
        log("缓存命中", dest.name)
        return dest

    import subprocess
    import tempfile
    errors: list[str] = []

    for i, repo in enumerate(repos):
        log("取源码" if i == 0 else "换镜像", f"{repo} @ {commit[:12]}")
        with tempfile.TemporaryDirectory() as td:
            work = Path(td) / "repo"
            try:
                subprocess.run(["git", "init", "-q", str(work)], check=True,
                               capture_output=True)
                subprocess.run(["git", "-C", str(work), "remote", "add",
                                "origin", repo], check=True, capture_output=True)
                # 先试只取这一个 commit（快）；服务器不允许按 sha 取时，
                # 退成完整 fetch。
                r = subprocess.run(
                    ["git", "-C", str(work), "fetch", "-q", "--depth", "1",
                     "origin", commit], capture_output=True, text=True)
                if r.returncode != 0:
                    subprocess.run(["git", "-C", str(work), "fetch", "-q",
                                    "origin"], check=True, capture_output=True)
                subprocess.run(["git", "-C", str(work), "checkout", "-q",
                                commit], check=True, capture_output=True)
                # 用纯 tar 不用 tgz：git archive 的 tar 输出是确定性的，
                # tgz 还要看 gzip 的头部时间。tarfile.open 按内容识别压缩，
                # 所以解压那边不用改。
                with open(dest, "wb") as f:
                    subprocess.run(
                        ["git", "-C", str(work), "archive", "--format=tar",
                         f"--prefix={_repo_name(repo)}-{commit[:12]}/", commit],
                        check=True, stdout=f)
            except subprocess.CalledProcessError as exc:
                detail = (exc.stderr or b"").decode("utf-8", "ignore")[:200]
                errors.append(f"{repo}\n           {detail.strip()}")
                dest.unlink(missing_ok=True)
                continue
        log("校验", "git 已按 commit 校验内容")
        return dest

    dest.unlink(missing_ok=True)
    die(f"{pkg['name']}: 所有 git 源都取不到 {commit[:12]}\n         "
        + "\n         ".join(errors))


# -------------------------------------------------------------- 下载校验

def _candidates(spec: dict, src: dict) -> list[str]:
    """按顺序返回该试的 URL：`url` 优先，然后是 `urls` 里的备选。

    为什么要多个源：上游会宕、会被墙、会慢。实测 `ftp.gnu.org` 在这台机器上
    就时通时不通——同一个地址，构建 glibc 时是通的，加 patch 配方时就不通了。

    **sha256 才是身份，URL 只是提示。** 所以换源不降低信任：不管从哪拿到，
    内容对不上就拒绝。
    """
    out: list[str] = []
    if src.get("url"):
        out.append(expand(spec, src["url"]))
    for u in src.get("urls", []):
        e = expand(spec, u)
        if e not in out:
            out.append(e)

    if not out:
        die(f"{spec['package']['name']}: [[source]] 缺少 url")
    return out


def fetch(spec: dict) -> Path:
    pkg = spec["package"]
    sources = spec.get("source") or []
    if not sources:
        die(f"{pkg['name']}: 配方没有声明 source")

    src = sources[0]

    # git 形式：说清仓库和 commit，取法由驱动决定。
    if src.get("git"):
        return fetch_git(spec, src)

    urls = _candidates(spec, src)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    # 文件名取第一个 URL 的。镜像站对同一个文件用的名字是一样的——GNU 的
    # 镜像就是这样，所以这不成问题。
    dest = CACHE_DIR / urls[0].rsplit("/", 1)[-1]

    if dest.is_file():
        log("下载", f"缓存命中 {dest.name}")
    else:
        tmp = dest.with_name(dest.name + ".part")
        errors: list[str] = []
        for i, url in enumerate(urls):
            log("下载" if i == 0 else "换源", url)
            try:
                urllib.request.urlretrieve(url, tmp)
            except Exception as exc:                    # noqa: BLE001
                errors.append(f"{url}\n           {type(exc).__name__}: {exc}")
                tmp.unlink(missing_ok=True)
                continue
            tmp.rename(dest)
            break
        else:
            die(f"{pkg['name']}: 所有源都取不到\n       "
                + "\n       ".join(errors))

    want = str(src.get("sha256", "")).strip().lower()
    if len(want) != 64:
        die(f"{pkg['name']}: 源码没有声明有效的 sha256"
            f"（不接受未校验的源码，当前值: {want!r}）")

    got = sha256_of(dest)
    if got != want:
        # 校验失败【不换源重试】：内容不对不是网络问题，是配方或镜像错了。
        # 悄悄换一个源继续，只会把「镜像被投毒」变成「构建成功」。
        die(f"{pkg['name']}: sha256 不匹配\n"
            f"       期望 {want}\n"
            f"       实际 {got}\n"
            f"       文件 {dest}")
    log("校验", "sha256 匹配")
    return dest


def extract(archive: Path, into: Path) -> str:
    """解压到 into，返回源码根目录相对名（'.' 表示就是 into 本身）。"""
    rmtree_force(into)
    into.mkdir(parents=True)
    log("解压", archive.name)
    with tarfile.open(archive) as tf:
        try:
            tf.extractall(into, filter="data")
        except TypeError:      # Python < 3.12
            tf.extractall(into)
    entries = list(into.iterdir())
    if len(entries) == 1 and entries[0].is_dir():
        return entries[0].name
    return "."


