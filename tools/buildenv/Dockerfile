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
        meson \
        ninja-build \
        cmake \
        ca-certificates \
        xz-utils \
        zstd \
        patch \
    && rm -rf /var/lib/apt/lists/*
