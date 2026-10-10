# YojigenOS 构建环境
#
# 注意：这里用 Debian 只是把它当「宿主」借工具链。
# 它不是你发行版的一部分——到工具链自举阶段，这个镜像会被
# YojigenOS 自己的系统取代。那时才叫真正的自举。

FROM debian:stable-slim

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        pkg-config \
        autoconf \
        automake \
        libtool \
        m4 \
        gettext \
        bison \
        flex \
        gawk \
        meson \
        ninja-build \
        cmake \
        ca-certificates \
        xz-utils \
        zstd \
        patch \
        rsync \
        ccache \
    && rm -rf /var/lib/apt/lists/*

# ccache 的代理目录放到 PATH 最前。那里有 gcc / g++ / cc / c++ 的符号链接，
# 所有编译调用自动走缓存——配方和 steps.sh 一个字都不用改。
ENV PATH=/usr/lib/ccache:$PATH

# 缓存目录由 skirt 从宿主机挂进来（工作区根目录下的 ccache/）。
#
# 为什么不用 Docker 命名卷：docker volume prune 和磁盘清理会把它一起扫掉，
# 而缓存的全部价值就在"能留住"。挂在宿主目录里，它跟着工作区走，
# 也能看见、能备份、能手动删。
ENV CCACHE_DIR=/ccache

# 上限。
#
# 实测：glibc 冷构建（三千多个文件）只产出几十 MB，全量 22 个包估计 1~2 GB。
# 所以 16 GB 短期内用不满——它的作用是等包多起来（gcc、python、perl、cmake
# 这些大块头进来）之后，不触发 LRU 淘汰把旧条目挤出去。
#
# 注意这也是最坏情况下的磁盘占用：ccache 不会预分配，但真涨到这个量级时
# 它会实打实吃掉这么多。宿主机磁盘紧张时调小，代价是命中率下降。
ENV CCACHE_MAXSIZE=16G
