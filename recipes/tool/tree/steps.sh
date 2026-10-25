# tree —— 目录树显示
#
# ★ 它只有 Makefile，没有 configure（实测解包看过：tree-2.2.1/ 里是
#   Makefile + INSTALL + tree.c，没有 configure / meson.build）。
#
# ★★ 而它的源码包是 **GitHub archive**：解出来是 `tree-2.2.1/` 一整个目录，
#   而不是"平铺进当前目录"。驱动已经把源码解到 $SRCDIR，所以这里多一层。
#
# ★ 它也不用 `--disable-nls`：tree 有 po/ 目录（zh_CN 在其中）。

build() {
    cd "$(ls -d "$SRCDIR"/tree-* 2>/dev/null | head -1)"
    make CC="${CC:-gcc}" \
         CFLAGS="${CFLAGS:--O2}" \
         LDFLAGS="${LDFLAGS:--s}" \
         prefix=/usr
}

package() {
    cd "$(ls -d "$SRCDIR"/tree-* 2>/dev/null | head -1)"
    # ★ 不用 upsteam 的 install target —— 它写死到 /usr/local（见 Makefile 的
    #   `MANDIR=${PREFIX}/man`，而 PREFIX 默认 /usr/local）。直接自己摆。
    install -Dm755 tree "$PKGDEST/usr/bin/tree"
    install -Dm644 tree.1 "$PKGDEST/usr/share/man/man1/tree.1"
    # 中文手册（上游带了）
    for l in zh_CN zh_TW; do
        [ -f "doc/$l/tree.1" ] && \
            install -Dm644 "doc/$l/tree.1" "$PKGDEST/usr/share/man/$l/man1/tree.1"
    done
    # ★ 而 .mo 由上游的 po/ 编译出来 —— 走 NLS 那一套，见下面那几行
    if [ -d po ]; then
        for p in po/*.po; do
            [ -f "$p" ] || continue
            lang=$(basename "$p" .po)
            msgfmt -o "/tmp/$lang.mo" "$p" 2>/dev/null && \
                install -Dm644 "/tmp/$lang.mo" "$PKGDEST/usr/share/locale/$lang/LC_MESSAGES/tree.mo"
        done
    fi
}
