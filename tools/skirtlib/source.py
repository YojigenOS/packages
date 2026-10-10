"""源码获取：下载、sha256 校验、解压。"""

from __future__ import annotations

import tarfile
import urllib.request
from pathlib import Path

from .common import CACHE_DIR, die, log, rmtree_force, sha256_of

from .spec import expand

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


