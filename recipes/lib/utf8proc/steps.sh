# utf8proc 的构建步骤
#
# 手写 Makefile。默认 prefix 是 /usr/local，要覆盖成 /usr。
#
# ★ 它的 ABI 版本是 3.3.0（Makefile 里 MAJOR=3 MINOR=3 PATCH=0），
#   所以 .so 叫 libutf8proc.so.3.3.0。这个【不去猜】——装完看实际产出了什么。

package() {
    make -C "$SRCDIR" install DESTDIR="$PKGDEST" prefix=/usr libdir=/usr/lib

    # ★ 不写死 .so 的名字。它的 ABI 版本会随着上游变（MAJOR.MINOR.PATCH），
    #   写死了下次升级就炸。查"有没有 libutf8proc.so*"这件事本身。
    so=$(ls "$PKGDEST"/usr/lib/libutf8proc.so* 2>/dev/null | head -1 || true)
    if [ -z "$so" ]; then
        echo "  ✗ /usr/lib 下没有 libutf8proc.so* —— prefix/libdir 那两个变量多半没生效"
        echo "     实际装出来的："
        find "$PKGDEST" -name "libutf8proc*" 2>/dev/null | head -6 | sed "s|^|       |" || true
        exit 1
    fi
    echo "  ✓ 装出了 $(basename "$so")"

    # 头文件也要在——tmux 编的时候要它。
    [ -f "$PKGDEST/usr/include/utf8proc.h" ] || {
        echo "  ✗ utf8proc.h 不在。tmux 编不过的。"; exit 1; }
    echo "  ✓ utf8proc.h 在"
}
