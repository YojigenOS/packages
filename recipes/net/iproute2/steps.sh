# iproute2 的构建步骤
#
# 它是手写的 Makefile，不是 autotools。但它带一个 configure 脚本
# （问你要不要 libelf、要不要 SELinux 之类），所以两步都走一遍。
#
# ★ 那个 configure 是【可选的】：不跑它 make 也能编，只是会用默认值。
#   跑它是为了明确表达"要 libelf、不要 SELinux"。

build() {
    cd "$SRCDIR"

    # 有 configure 就跑。它不长在 /bin/sh 之外的任何东西上（就是个 sh 脚本）。
    [ -x ./configure ] && ./configure
    [ -f ./configure ] && [ ! -x ./configure ] && sh ./configure

    make
}

package() {
    cd "$SRCDIR"
    make DESTDIR="$PKGDEST" install

    # 至少要有一个 ip 和 ss。查这两个，别查一长串——
    # 它装的工具比这多得多，但这两个是"这套东西能用"的证据。
    for f in usr/sbin/ip usr/bin/ss usr/sbin/ss; do
        [ -e "$PKGDEST/$f" ] && { echo "  ✓ 有 $f"; found=1; }
    done
    [ -n "$found" ] || { echo "  ✗ ip/ss 都不在——安装前缀多半不对"; exit 1; }
}
