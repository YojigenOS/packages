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
#
# ★★★ 而它**不带 pkg-config 的 .pc 文件** —— 实测：上游 tar 里没有
#   liblmdb.pc.in，装出来的 /usr/lib/pkgconfig 是空的。
#
#   而 bind9 的 meson.build 第 953 行是：
#       lmdb_dep = dependency('lmdb')          ← 靠 pkg-config 找
#   于是构建时：
#       Run-time dependency lmdb found: NO
#       meson.build:953:11: ERROR: Dependency "lmdb" not found
#
#   所以我们自己补一个 .pc。这不是"绕过检查"—— Debian 的 liblmdb-dev
#   就是带 .pc 的（他们是自己写的），而**消费方（bind9）按 pkg-config
#   找是上游既定的事实**。

build() {
    cd "$SRCDIR/libraries/liblmdb"
    make CC="${CC:-gcc}" AR="${AR:-ar}" RANLIB="${RANLIB:-ranlib}"
}

package() {
    cd "$SRCDIR/libraries/liblmdb"
    make install DESTDIR="$PKGDEST" prefix=/usr

    # ★ 补 pkg-config 文件（上游不提供，而 bind9 的 meson 靠它找）
    #
    # ★★ Version 写 0.9.34。这个值来自 lmdb.h 的 MDB_VERSION_* 三个宏：
    #     MDB_VERSION_MAJOR 0 / MINOR 9 / PATCH 34
    #   而 OpenLDAP 自己的版本是 2.7.2 —— **两个不是一回事**，别搞混。
    install -d "$PKGDEST/usr/lib/pkgconfig"
    cat > "$PKGDEST/usr/lib/pkgconfig/lmdb.pc" <<'PC'
prefix=/usr
exec_prefix=${prefix}
libdir=${exec_prefix}/lib
includedir=${prefix}/include

Name: lmdb
Description: Lightning Memory-Mapped Database (embedded key-value store)
Version: 0.9.34
Libs: -L${libdir} -llmdb
Cflags: -I${includedir}
PC
}
