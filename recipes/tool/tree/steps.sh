# tree —— 目录树显示
#
# ★ 它只有 Makefile，没有 configure（实测解包看过：Makefile + INSTALL +
#   tree.c，没有 configure / meson.build）。
#
# ★★ `$SRCDIR` **就是源码根**（驱动算好的，见 build.py 的 `SRCDIR="$WORK/src/{srcrel}"`）。
#   所以这里不 cd —— 第一版我写了 `cd "$SRCDIR"/tree-*`，结果是：
#
#       /build/steps.sh: line 12: cd: null directory
#
#   因为 GitHub archive 解出来之后驱动已经把那一层剥掉了，$SRCDIR 里就是
#   Makefile 所在目录。
#
# ★ 它没有 `--disable-nls`：上游 po/ 有 zh_CN，make 时会自己编（要 msgfmt，
#   所以构建依赖里有 gettext 的运行时 —— 而 gettext 在 devel 闭包里）。
#
# ★★ 而它的 `make install` 不能用：Makefile 里 `MANDIR=${PREFIX}/man`，而
#    PREFIX 默认 /usr/local。直接自己摆文件更可控，也顺手把中文手册装上。

build() {
    make CC="${CC:-gcc}" \
         CFLAGS="${CFLAGS:--O2}" \
         LDFLAGS="${LDFLAGS:--s}" \
         prefix=/usr
}

package() {
    install -Dm755 tree "$PKGDEST/usr/bin/tree"
    install -Dm644 tree.1 "$PKGDEST/usr/share/man/man1/tree.1"
    # ★ 中文手册（上游带了 doc/zh_CN 和 doc/zh_TW）
    for l in zh_CN zh_TW; do
        [ -f "doc/$l/tree.1" ] && \
            install -Dm644 "doc/$l/tree.1" "$PKGDEST/usr/share/man/$l/man1/tree.1"
    done
    # ★ 翻译（.mo）由 make 在 build() 里编好，装在 po/*.gmo → 这里搬过来
    for m in po/*.gmo; do
        [ -f "$m" ] || continue
        lang=$(basename "$m" .gmo)
        install -Dm644 "$m" "$PKGDEST/usr/share/locale/$lang/LC_MESSAGES/tree.mo"
    done
}
