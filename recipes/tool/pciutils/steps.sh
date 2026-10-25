# pciutils —— PCI 设备查看（lspci / setpci）
#
# ★ 它只有一个裸 Makefile，没有 configure。而它的 Makefile 认这些变量：
#     PREFIX  安装前缀
#     DESTDIR 暂存目录
#     SHARED  要不要建 .so（0/1）
#     ZLIB    要不要 zlib（我们依赖里有）
#     PCI_IDS 要不要装 pci.ids（设备名数据库，**必须**，否则 lspci 只显示数字 ID）
#
# ★★ 它的 `make install` 会装到 `$DESTDIR/$PREFIX/...`，所以两个都给。

build() {
    make CC="${CC:-gcc}" \
         OPT="${CFLAGS:--O2}" \
         PREFIX=/usr \
         SHARED=0 \
         ZLIB=yes \
         lspci setpci
}

package() {
    # ★ ZLIB=yes 需要 zlib 的头；而 `pci.ids` 是设备名数据库 ——
    #   没有它 lspci 只打 `8086:1234` 这种裸 ID，对排障几乎没用。
    make install DESTDIR="$PKGDEST" PREFIX=/usr SHARED=0 ZLIB=yes
}
