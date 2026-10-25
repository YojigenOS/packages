# whois —— WHOIS 客户端
#
# ★ 它只有 Makefile，没有 configure。而源码包解出来是 `work/` 目录
#   （Debian 的 `whois_5.6.6.tar.xz` 是 salsa 的源码树快照，不是上游 tar）。
#
# ★ 它的 Makefile 里 `prefix = /usr` 已经是我们要的，所以直接 make install
#   加 DESTDIR 就行。

build() {
    cd "$SRCDIR/work" 2>/dev/null || cd "$(ls -d "$SRCDIR"/whois-* 2>/dev/null | head -1)"
    make CC="${CC:-gcc}" CFLAGS="${CFLAGS:--O2}"
}

package() {
    cd "$SRCDIR/work" 2>/dev/null || cd "$(ls -d "$SRCDIR"/whois-* 2>/dev/null | head -1)"
    make install DESTDIR="$PKGDEST" prefix=/usr
}
