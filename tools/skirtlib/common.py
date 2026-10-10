"""路径常量、日志、错误退出、哈希、架构名——最底层的公共设施。"""

from __future__ import annotations

import hashlib
import os
import platform
import shutil
import sys
import time
from pathlib import Path

# 模块在 tools/skirtlib/ 下，仓库根是上三级
ROOT = Path(__file__).resolve().parent.parent.parent
PACKAGES_DIR = ROOT / "packages"
OUTPUT_DIR = ROOT / "output"
CACHE_DIR = ROOT / "cache"
BUILD_DIR = ROOT / "build"
HOST_PROVIDES_FILE = ROOT / "host" / "provides.txt"
BUILDENV_IMAGE = "yojigen/buildenv"

ARCH_ALIASES = {
    "arm64": "aarch64",
    "aarch64": "aarch64",
    "x86_64": "x86_64",
    "amd64": "x86_64",
}

# --------------------------------------------------------------- 基础工具

def log(stage: str, msg: str) -> None:
    print(f"\033[1m[{stage}]\033[0m {msg}", flush=True)


def die(msg: str) -> None:
    sys.stdout.flush()      # 否则 stdout 里的进度会冒到错误信息后面
    print(f"\033[31mskirt: 错误\033[0m {msg}", file=sys.stderr)
    sys.exit(1)


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def rmtree_force(path: Path) -> None:
    """删除目录树，带重试。

    在 macOS + Docker 绑定挂载下，容器刚退出时文件系统状态可能尚未同步完，
    shutil.rmtree 内部的 os.rmdir 会偶发 "Directory not empty"——即使目录
    实际上已经被掏空了。重试可以跨过这个窗口。
    """
    if not path.exists():
        return
    last: OSError | None = None
    for attempt in range(5):
        try:
            shutil.rmtree(path)
            return
        except OSError as exc:
            last = exc
            time.sleep(0.5 * (attempt + 1))
    die(f"清理失败: {path}\n       {last}\n"
        f"       这通常只是文件系统同步慢，手动删除该目录后重试即可。")


def resolve_arch(requested: str | None) -> str:
    host = ARCH_ALIASES.get(platform.machine())
    if host is None:
        die(f"不认识的宿主架构: {platform.machine()}")
    if requested is None:
        return host
    want = ARCH_ALIASES.get(requested, requested)
    if want != host:
        die(
            f"暂不支持交叉构建：宿主是 {host}，请求的是 {want}。\n"
            f"       交叉工具链要等工具链自举之后才会加上，见 docs/ 里的架构策略。"
        )
    return want


