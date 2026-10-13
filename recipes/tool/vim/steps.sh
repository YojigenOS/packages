# vim 的构建步骤
#
# 它的 configure 在 src/ 下面，不是根目录——所以走 custom。
#
# 【为什么不要 GUI 也不要 Python 支持】
#
# 服务器上要的是那个能改配置文件的 vim。--with-features=normal 是标准的
# "没有 GUI 但有 :syntax on 和 :help"那一档；--enable-gui=no 明确关掉
# GUI（不然它可能去找 X11 的头文件，然后在构建结尾报一个看不懂的错）。
#
# ★ --with-tlib=ncursesw
#   不加这条它可能去找 libtinfo（ncurses 拆出来的那个），而我们没有拆。
#   我们的 ncurses 是整的，用 --enable-widec 编的，所以库名带 w。

build() {
    cd "$SRCDIR/src"
    ./configure \
        --prefix=/usr \
        --with-features=normal \
        --enable-gui=no \
        --enable-multibyte \
        --with-tlib=ncursesw \
        --disable-nls \
        --disable-netbeans
    make
}

package() {
    cd "$SRCDIR/src"
    make DESTDIR="$PKGDEST" install

    # vim 的 install 会装一个 vim 和一个 vi（指向 vim 的链接）。
    # 两者缺一不可：脚本里写 vi 的比写 vim 的多。
    for f in usr/bin/vim usr/bin/vi; do
        [ -e "$PKGDEST/$f" ] || { echo "  ✗ $f 不在"; exit 1; }
    done
    echo "  ✓ vim 和 vi 都在"
}
