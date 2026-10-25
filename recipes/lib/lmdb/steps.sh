# lmdb —— 从 OpenLDAP 源码树的 libraries/liblmdb/ 编
#
# ★ 那一层自带 Makefile（不是 autotools），所以只 cd 进去 make。
#   实测过：mdb.c / lmdb.h / Makefile 都在。
#
# ★★ 它认这些变量（见那个 Makefile）：
#       prefix   /usr
#       DESTDIR 暂存
#       CC      编译器
#       AR/RANLIB

build() {
    cd "$(ls -d "$SRCDIR"/openldap-* 2>/dev/null | head -1)/libraries/liblmdb"
    make CC="${CC:-gcc}" AR="${AR:-ar}" RANLIB="${RANLIB:-ranlib}"
}

package() {
    cd "$(ls -d "$SRCDIR"/openldap-* 2>/dev/null | head -1)/libraries/liblmdb"
    # ★ prefix 要传进去 —— 那个 Makefile 的默认是 /usr/local
    make install DESTDIR="$PKGDEST" prefix=/usr
}
