"""源码获取：下载、sha256 校验、解压。"""

from __future__ import annotations

import tarfile
import urllib.request
from pathlib import Path

from .common import CACHE_DIR, die, log, rmtree_force, sha256_of

from .spec import expand

# -------------------------------------------------------------- 下载校验

def fetch(spec: dict) -> Path:
    pkg = spec["package"]
    sources = spec.get("source") or []
    if not sources:
        die(f"{pkg['name']}: 配方没有声明 source")

    src = sources[0]
    url = expand(spec, src["url"])
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    dest = CACHE_DIR / url.rsplit("/", 1)[-1]

    if dest.is_file():
        log("下载", f"缓存命中 {dest.name}")
    else:
        log("下载", url)
        tmp = dest.with_name(dest.name + ".part")
        urllib.request.urlretrieve(url, tmp)
        tmp.rename(dest)

    want = str(src.get("sha256", "")).strip().lower()
    if len(want) != 64:
        die(f"{pkg['name']}: 源码没有声明有效的 sha256"
            f"（不接受未校验的源码，当前值: {want!r}）")

    got = sha256_of(dest)
    if got != want:
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


