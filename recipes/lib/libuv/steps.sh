# libuv —— 跨平台异步 I/O 库
#
# ★★★ 上游 tar 里**没有预生成的 configure**。实测 `tar -tzf` 看到的是：
#       libuv-v1.53.0/autogen.sh      ← 要先跑它
#       libuv-v1.53.0/configure.ac
#       libuv-v1.53.0/Makefile.am
#
#   所以驱动那句 `./configure` 会直接失败：
#       /build/run.sh: line 5: ./configure: No such file or directory
#
# ★ 而 `autogen.sh` 要 autoreconf + libtool（依赖里已经有 autoconf /
#   automake / libtool）。
#
# ★★ 它要 liburcu（Linux 上）—— 那在 runtime 依赖里，而驱动会把
#   依赖的 payload 合并进 /（日志里那句"合并 217 项到 /"就是它）。

build() {
    # ★ autogen.sh 会生成 configure。它内部调 autoreconf + libtoolize。
    #   而它的 shebang 是 #!/bin/sh —— 用 sh 跑最稳（不依赖它在不在 PATH）。
    sh ./autogen.sh

    ./configure \
        --prefix=/usr \
        --disable-static
    make -j"${JOBS:-4}"
}

package() {
    make install DESTDIR="$PKGDEST"
}
