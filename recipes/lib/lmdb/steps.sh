# lmdb —— 从 OpenLDAP 源码树的 libraries/liblmdb/ 编
#
# ★ `$SRCDIR` 是 OpenLDAP 的源码根（驱动算的），而 lmdb 在它下面一层：
#       $SRCDIR/libraries/liblmdb/
#   所以这里**要** cd —— 这和其他包不一样，因为 lmdb 没有独立 tar。
#
# ★★ 那一层自带 Makefile（不是 autotools），认这些变量：
#       prefix   /usr（默认 /usr/local —— 要显式传）
#       DESTDIR  暂存目录
#       CC / AR / RANLIB

build() {
    cd "$SRCDIR/libraries/liblmdb"
    make CC="${CC:-gcc}" AR="${AR:-ar}" RANLIB="${RANLIB:-ranlib}"
}

package() {
    cd "$SRCDIR/libraries/liblmdb"
    make install DESTDIR="$PKGDEST" prefix=/usr
}
