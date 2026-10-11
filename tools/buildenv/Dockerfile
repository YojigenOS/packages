# YojigenOS 构建环境
#
# 注意：这里用 Debian 只是把它当「宿主」借工具链。
# 它不是你发行版的一部分——到工具链自举阶段，这个镜像会被
# YojigenOS 自己的系统取代。那时才叫真正的自举。

FROM debian:stable-slim

ENV DEBIAN_FRONTEND=noninteractive

# Debian 的镜像站。
#
# 默认是官方的 deb.debian.org，CI 上用它。但在国内它经常连不上——实测
# amd64 容器里报 "Connection failed [IP: 11.35.251.233 80]"，那是 DNS 污染
# 给出的假地址。本地要建别的架构的构建环境时（走 Rosetta）就得换一个。
#
#     docker build --build-arg DEBIAN_MIRROR=http://mirrors.ustc.edu.cn \
#                  -t yojigenos/buildenv:x86_64 tools/buildenv
#
# **用 http 不用 https**：debian:stable-slim 里还没有 ca-certificates，
# 所以 https 会因为证书验证失败而连不上（实测 "SSL connection failed"）。
# apt 靠签名而不是靠 TLS 保证完整性，http 是安全的。
#
# 换来换去不影响产物：装的是同一批包，只是从哪儿拿的区别。
ARG DEBIAN_MIRROR=http://deb.debian.org
RUN if [ "$DEBIAN_MIRROR" != "http://deb.debian.org" ]; then \
        sed -i "s|http://deb.debian.org|$DEBIAN_MIRROR|g" \
            /etc/apt/sources.list.d/debian.sources 2>/dev/null || \
        sed -i "s|http://deb.debian.org|$DEBIAN_MIRROR|g" /etc/apt/sources.list; \
    fi

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
