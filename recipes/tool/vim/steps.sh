# vim 的构建步骤
#
# 它的 configure 在 src/ 下面，不是根目录 —— 所以走 custom。

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

    # ★ 只对 vim 【报错】—— 那是这个包的本体。
    [ -x "$PKGDEST/usr/bin/vim" ] || { echo "  ✗ /usr/bin/vim 不在"; exit 1; }
    echo "  ✓ vim 装好了"

    # ★ 而 vi 是【另一回事】。
    #
    #   我第一版把两个都写成"必须存在"，于是这一格挂在：
    #
    #       ✗ usr/bin/vi 不在
    #
    #   而那时 vim 已经完整装好了（整个 runtime 都拷进去了，日志里能看到）。
    #
    #   `vi` 是上游在某些配置下才建的符号链接。**把它当成硬性要求是我的判断，
    #   不是上游的约定** —— 而那个判断拦在交付物前面，等于因为一个可以后补的
    #   细节否掉整个包。
    #
    #   正确的事是：既然我们是一个发行版，而脚本里写 `vi` 的比写 `vim` 的多，
    #   那就【我们自己建这个链接】—— 而不是要求上游建。
    ln -sf vim "$PKGDEST/usr/bin/vi"

    [ -x "$PKGDEST/usr/bin/vi" ] || { echo "  ✗ vi 链接没建成"; exit 1; }
    echo "  ✓ vi → vim（这个链接是我们建的）"
}
