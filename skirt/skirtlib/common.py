"""路径常量、日志、错误退出、哈希、架构名——最底层的公共设施。"""

from __future__ import annotations

import hashlib
import os
import platform
import shutil
import sys
import time
from pathlib import Path

# 模块在 skirt/skirtlib/ 下，仓库根是上三级
ROOT = Path(__file__).resolve().parent.parent.parent
RECIPES_DIR = ROOT / "recipes"
VARIANTS_DIR = ROOT / "variants"
OUTPUT_DIR = ROOT / "var" / "output"
CACHE_DIR = ROOT / "var" / "cache"
BUILD_DIR = ROOT / "var" / "work"
#: 每个包的完整构建日志。放这里是因为失败时要能翻——见 commands.py 里的说明。
LOG_DIR = ROOT / "var" / "logs"
HOST_PROVIDES_FILE = ROOT / "bootstrap" / "provides.txt"
#: 构建环境镜像。默认是 Debian 底的那个（bootstrap 构建出来的）。
#:
#: 用 SKIRT_BUILDENV 可以换掉它——这正是自举的验证方式：把 base-devel 拼出来
#: 导入成镜像，然后 `SKIRT_BUILDENV=yojigenos/base-devel ./skirt/skirt build ...`。
#: 跑得通，就说明我们的系统能构建我们自己的东西；跑不通，第一个报错就是答案。
BUILDENV_IMAGE = os.environ.get("SKIRT_BUILDENV", "yojigenos/buildenv")

# ccache 缓存放在仓库外面（工作区根目录下）。
#
# 两个理由：它体积大且完全可再生，不该进版本控制；同时也不能放 Docker
# 命名卷——docker volume prune 和磁盘清理会连它一起扫掉，而缓存的全部
# 价值就在「能留住」。挂在宿主目录里，它跟着工作区走，能看见、能备份。
# 用环境变量 SKIRT_CCACHE 覆盖。
CCACHE_DIR = Path(os.environ.get("SKIRT_CCACHE", ROOT.parent / "ccache"))
# GNU 包的备用镜像写在【配方】里，驱动不补。
#
# 我原来在驱动里加过一段「自动给 ftp.gnu.org 的包补镜像」，理由是 21 个包改
# 21 处太啰嗦。那个理由不足以抵消它的坏处：**配方应该是自包含的**——读一个
# 配方就该知道它的源码全部从哪来。驱动偷偷加源会让行为不可见，排查时你会问
# "它为什么从 USTC 下载？我没写这个"。
#
# Arch 也是这么分的：PKGBUILD 里写上游，镜像配在 /etc/pacman.d/mirrorlist。
# 配方那份是【身份】，系统那份是【政策】。
#
# 而"中美环境不同"这个担心不成立：`url`（上游）排在第一位，镜像只是兜底——
# ftp.gnu.org 在两地都不算慢（实测本机 2.9 秒），所以镜像在 CI 上永远轮不到。

#: 缓存在容器里看到的目录。宿主的 CCACHE_DIR 挂到这里。
#:
#: 驱动会把它显式传进容器（`-e CCACHE_DIR=...`），不靠镜像的 ENV——
#: base-devel 是 docker import 出来的，那种镜像不带 ENV。
CCACHE_MOUNT = "/ccache"

#: 缓存上限。16 GB 是按最坏情况定的：gcc 一个包的中间产物就有几个 GB。
CCACHE_MAXSIZE = os.environ.get("SKIRT_CCACHE_MAXSIZE", "16G")

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
    if want not in ARCH_ALIASES.values():
        die(f"不认识的架构: {requested}")
    # 【这里原来会拒绝非宿主架构】理由是"暂不支持交叉构建"。
    #
    # 那个理由把问题想复杂了：我们不是换工具链，是换容器。aarch64 的包在
    # x86_64 上构建，靠的是 `docker run --platform linux/x86_64` 跑一个
    # x86_64 的构建环境镜像——工具链是那个镜像里的，不是交叉工具链。
    #
    # 所以不拦。真正需要平台选择的是 run_in_container（那里加 --platform），
    # 而在原生 runner 上连那个都不需要。
    return want


def build_jobs() -> int:
    """本地构建的并行度：默认留两个核给用户。

    构建吃满 CPU 时，机器会卡到没法做别的事——而这台机器同时也是你写代码
    的地方。少两个核，gcc 大概慢 20%，但你能一边等一边继续干活。

    用环境变量 SKIRT_JOBS 覆盖（CI 上想全开就设成核数）。
    """
    override = os.environ.get("SKIRT_JOBS")
    if override:
        try:
            n = int(override)
        except ValueError:
            die(f"SKIRT_JOBS 不是整数: {override!r}")
        if n < 1:
            die(f"SKIRT_JOBS 必须 ≥ 1，收到 {n}")
        return n

    total = os.cpu_count() or 4
    return max(1, total - 2)


