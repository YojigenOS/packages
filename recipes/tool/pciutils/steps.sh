# pciutils —— PCI 设备查看（lspci / setpci）
#
# ★ 它只有一个裸 Makefile，没有 configure。而它的 Makefile 认这些变量：
#     PREFIX  安装前缀
#     DESTDIR 暂存目录
#     SHARED  要不要建 .so（yes/no）—— ★ 必须是 yes，见下面那段
#     ZLIB    要不要 zlib（我们依赖里有）
#     PCI_IDS 要不要装 pci.ids（设备名数据库，**必须**，否则 lspci 只显示数字 ID）
#
# ★★ 它的 `make install` 会装到 `$DESTDIR/$PREFIX/...`，所以两个都给。
#
# ★★★ 而 `SHARED` **必须是 yes**（2026-10-26 撞到的）：
#
#   第一版我写了 `SHARED=0`（想"只建静态库，少一个 .so"）。后果是
#   `lspci` 那个二进制**仍然链接了 `libpci.so.3`**，而包里没有那个 .so ——
#   于是装的时候：
#
#       ERROR: unable to select packages:
#         so:libpci.so.3 (no such package):
#           required by: pciutils-3.15.0-r1[so:libpci.so.3]
#
#   ★ 判据：**"要不要 .so"不是能随便选的** —— 上游的 Makefile 里
#     `lspci` 和 `setpci` 是链 `libpci.so` 的，`SHARED=0` 只是不建那个 .so，
#     不改变链接方式。这种"看起来能省一个文件"的开关，实际会做出一个
#     自己装不上自己的包。

build() {
    make CC="${CC:-gcc}" \
         OPT="${CFLAGS:--O2}" \
         PREFIX=/usr \
         SHARED=yes \
         ZLIB=yes \
         lspci setpci
}

package() {
    # ★ ZLIB=yes 需要 zlib 的头；而 `pci.ids` 是设备名数据库 ——
    #   没有它 lspci 只打 `8086:1234` 这种裸 ID，对排障几乎没用。
    make install DESTDIR="$PKGDEST" PREFIX=/usr SHARED=yes ZLIB=yes
}
