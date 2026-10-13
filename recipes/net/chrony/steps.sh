# chrony 的构建步骤。
#
# ★ 它是【手写 configure】的项目，不是 autotools —— 所以走 custom，
#   每一步都明说，不套驱动对 autotools 的假设。

build() {
    cd "$SRCDIR"

    # ★ 三件必须显式说的事：
    #
    #   ① --prefix=/usr
    #      它的默认是 /usr/local，而那和我们其余所有包都不一致。
    #
    #   ② --sbindir=/usr/sbin
    #      ★ 和 iproute2 同一类：chronyd / chronyc 要落在 /usr/sbin，
    #        而它的默认是 $prefix/sbin（也就是 /usr/sbin）—— 但显式写，
    #        这样"装在哪儿"是一个决定而不是一个默认值。
    #
    #   ③ 我们【不用】 --libdir：它不是 GNU configure，那个开关它不认。
    #      库落在哪里由它的 Makefile.in 定（chrony 本身没有对外库）。
    ./configure \
        --prefix=/usr \
        --sysconfdir=/etc \
        --localstatedir=/var \
        --sbindir=/usr/sbin \
        --without-readline \
        --without-editline \
        --disable-nts \
        --disable-sechash \
        --disable-scfilter \
        --disable-nls

    make
}

package() {
    cd "$SRCDIR"
    make DESTDIR="$PKGDEST" install

    for f in usr/sbin/chronyd usr/sbin/chronyc; do
        [ -x "$PKGDEST/$f" ] || { echo "  ✗ $f 不在"; exit 1; }
    done
    echo "  ✓ chronyd 和 chronyc 都在 /usr/sbin"
}
