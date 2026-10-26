# ncurses —— 补上不带 `w` 后缀的兼容软链接
#
# ★★★ 为什么需要这一步（2026-10-26）：
#
#   `--enable-widec` 让 ncurses 编出 **wide** 版，而库名带 `w` 后缀：
#
#       libncursesw.so.6 · libtinfow.so.6 · libformw.so.6 · libpanelw.so.6
#
#   而我们【不能】加 `--disable-lib-suffixes` 去掉那个后缀 —— 实测 TX 上：
#
#       bash      → NEEDED libncursesw.so.6
#       vim       → NEEDED libncursesw.so.6
#       readline  → NEEDED libncursesw.so.6
#
#   去掉后缀会让**这三个当场起不来**。
#
# ★★ 但一部分程序的 configure 探测的是**不带 w 的名字**。实测撞上的是 parted：
#
#       AC_SEARCH_LIBS([tgetent], [tinfo ncurses curses termcap termlib])
#
#   五个名字里没有 `tinfow` / `ncursesw`，所以一个都匹配不到：
#
#       configure: error: termcap could not be found which is required for the
#       --with-readline option
#
# ★ 所以两边都要：**wide 库是主库（保留 w 后缀），另加不带 w 的软链接**。
#   这也正是 Debian/Ubuntu 的做法 —— 它们的 libtinfo6 里同时有
#   `libtinfo.so.6` 和它指向 `libtinfow.so.6` 的关系（反过来也行）。
#
#   ★ 用软链接而不是真的编两套：软链接指向的就是同一份 wide 代码，
#     而 8-bit 和 wide 的 ABI 在"只传 ASCII"时是兼容的。**编两套要多一倍
#     体积，而收益只是"理论上的 8-bit 语义"—— 那个已经没人依赖了。**

build() {
    # ★★★ 自定义 build() 必须自己做完默认那一步。
    #   （`type = "autotools"` 的默认就是 configure + make，这里照抄。）
    #   ★ 而 configure 参数在 spec.toml 的 `[build] configure` 里，steps.sh
    #     读不到那个数组 —— 所以这又是一处"两处维护同一份参数"。
    #     下一轮该让驱动 export 出来（CONFIGURE_OPTS）。
    ./configure \
        --prefix=/usr \
        --enable-widec \
        --with-shared \
        --with-termlib \
        --enable-termcap \
        --enable-sp-funcs \
        --without-debug \
        --enable-pc-files \
        --with-pkg-config-libdir=/usr/lib/pkgconfig \
        --without-ada \
        --without-cxx-binding \
        --disable-stripping \
        --with-default-terminfo-dir=/usr/share/terminfo \
        --with-terminfo-dirs=/usr/share/terminfo:/etc/terminfo
    make -j"${JOBS:-4}"
}

package() {
    cd "$SRCDIR"
    make install DESTDIR="$PKGDEST"

    # ★ 给每个 `libXw.so.N` 补一个 `libX.so.N` 软链接（同目录、指回 wide 那份）。
    #
    #   ★ 只补 .so.N 和 .so 两层（源码链接时找 .so，运行时装 .so.N）。
    #     不补 .so.N.M.P —— 那个名字由 soname 指向 .so.N，链一层就够。
    cd "$PKGDEST/usr/lib"
    for w in libncursesw libtinfow libformw libmenuw libpanelw; do
        base=${w%w}                      # libncursesw → libncurses
        for v in "" ".6"; do             # 只处理 .so 和 .so.6 两层
            src="$w.so$v"
            dst="$base.so$v"
            [ -e "$src" ] || continue
            [ -e "$dst" ] && continue     # 已经有了就不动
            ln -s "$src" "$dst"
        done
    done

    # ★ 验一遍：tgetent 必须能从 libtinfo.so 链上（那正是 parted 要的）。
    #   这一步只是报告，不失败 —— 链接测试要编译，而这里是 package 阶段。
    for f in libtinfo.so libncurses.so; do
        if [ -e "$f" ]; then
            echo "  ✓ $f 已补（→ $(readlink "$f")）" >&2
        else
            echo "  ⚠ $f 没补上 —— 那些探测 tgetent 的包会找不到" >&2
        fi
    done
}
