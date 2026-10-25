# dos2unix —— DOS/Unix 换行符转换
#
# ★ 它**有** Makefile 但没有 configure（上游用 `make -f Makefile` 直接编，
#   而 po/ 那套靠 gettext 的工具）。
#
# ★★ 它要 gettext 的 `msgfmt` 编翻译 —— 没有的话 make 会在 `make -C po`
#   时失败（不像 autotools 那样静默跳过）。

build() {
    make CC="${CC:-gcc}" CFLAGS="${CFLAGS:--O2}" prefix=/usr
}

package() {
    make install DESTDIR="$PKGDEST" prefix=/usr
}
