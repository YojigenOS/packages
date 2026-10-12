# pkgconf 的构建步骤
#
# build() 用驱动的 autotools 默认（选项见 spec.toml）。
#
# 这里只补一件事：**装出 `pkg-config` 这个名字**。
#
# 上游默认会装那个兼容名，但实测这份 tarball 的 autotools 路径【没有装】
# （payload 里只有 pkgconf / bomtool / pccritic / spdxtool）。而这个词是
# 【契约】：elfutils 的 configure 就是去找一个叫 pkg-config 的程序：
#
#     checking for pkg-config... no
#     configure: error: pkg-config not found
#
# 别人写构建脚本时也只会写 pkg-config。所以这个名字必须存在，由我们保证，
# 而不是指望上游的默认值。
#
# 可用的环境变量（由驱动注入）：$PKGDEST $PKGDIR $SRCDIR $WORK

package() {
    # 重定义 package() = 接管默认那一步，所以这行不能少
    make DESTDIR="$PKGDEST" install

    d="$PKGDEST"

    # pkg-config → pkgconf（相对符号链接）
    if [ -x "$d/usr/bin/pkgconf" ] && [ ! -e "$d/usr/bin/pkg-config" ]; then
        ln -s pkgconf "$d/usr/bin/pkg-config"
    fi

    # man 页同理：查 pkg-config(1) 的人得能找到东西
    if [ -f "$d/usr/share/man/man1/pkgconf.1" ] && [ ! -e "$d/usr/share/man/man1/pkg-config.1" ]; then
        ln -s pkgconf.1 "$d/usr/share/man/man1/pkg-config.1"
    fi
}
