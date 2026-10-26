# scdoc —— 裸 Makefile，认 PREFIX / DESTDIR
#
# ★ `$SRCDIR` 已经是源码根（驱动算的），不要 cd 进去。
#
# ★★ 它的 Makefile 里 `PREFIX = /usr/local`（默认），要显式传 /usr。

build() {
    make CC="${CC:-gcc}" CFLAGS="${CFLAGS:--O2}" PREFIX=/usr
}

package() {
    make install DESTDIR="$PKGDEST" PREFIX=/usr
}
