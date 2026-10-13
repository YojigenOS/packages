# libcap 的构建步骤
#
# 它不用 autotools 也不用 cmake，是手写的 Makefile。驱动只认四种类型，
# 所以走 custom，在这里把两件事写清楚：怎么编、怎么装。
#
# 【三个变量为什么是这个值】
#
#   GOLANG=no        关掉 Go 绑定。开着就要整个 Go 工具链，而我们要的是
#                    库和 setcap/getcap 工具，不是 Go 的那层包装。
#   PAM_CAP=no       关掉 pam_cap 模块。PAM 我们还没加（要用 PAM 的包先得
#                    把 PAM 本身加进来），开着这里会直接失败。
#   RAISE_SETFCAP=no 装的时候不跑 setcap 给二进制加权限。在构建容器里做这
#                    件事没意义（权限位存不进 apk），而且会污染产物。
#
# 这三个变量【编和装两遍都要传】——它 install 时还会再编一遍小工具，
# 少了变量那边就会走出不同的分支。

VARS="GOLANG=no PAM_CAP=no RAISE_SETFCAP=no prefix=/usr lib=lib"

build() {
    # shellcheck disable=SC2086
    make -C "$SRCDIR" $VARS
}

package() {
    # shellcheck disable=SC2086
    make -C "$SRCDIR" install DESTDIR="$PKGDEST" $VARS

    # ★ 装完检查一下东西真的落在了 /usr 下面。
    #
    #   它的 Makefile 默认 prefix 是 /usr/local，而我们传的 prefix=/usr ——
    #   如果那个变量名不对（或者被 Makefile 内部写死），产物会跑到
    #   /usr/local 下面，而 apk 里那样就是一个"装了但找不着"的包。
    #   这不是猜：prefix/lib 这两个名字是照着它 Makefile 里的用法传的。
    for f in usr/sbin/setcap usr/sbin/getcap usr/lib/libcap.so; do
        [ -e "$PKGDEST/$f" ] || { echo "  ✗ $f 不在——prefix/lib 那两个变量多半没生效"; exit 1; }
    done
    echo "  ✓ libcap 落在 /usr 下面了"
}
