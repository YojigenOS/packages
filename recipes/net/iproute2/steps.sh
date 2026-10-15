# iproute2 的构建步骤。
#
# 它是手写的 Makefile，不是 autotools。带一个可选的 configure
# （问你要不要 libelf、要不要 SELinux），所以两步都走一遍。

# ★ SBINDIR 必须显式给 —— 它的默认值是【绝对路径 /sbin】，不是 $(PREFIX)/sbin。
#
#   实测：
#
#       install -m 0755 genl /build/dest/sbin      ← 装进了 $PKGDEST/sbin
#       ✗ ip/ss 都不在——安装前缀多半不对
#
#   upstream 的 Makefile 里：
#
#       PREFIX?=/usr
#       SBINDIR?=/sbin          ← ★ 就这一个不管 PREFIX
#       DATADIR?=$(PREFIX)/share
#       HDRDIR?=$(PREFIX)/include/iproute2
#       CONF_ETC_DIR?=/etc/iproute2
#       NETNS_RUN_DIR?=/var/run/netns     （后面这些是运行时路径，不是安装路径）
#
#   于是包里的产物是一个【真目录 /sbin】，而我们系统的 /sbin 是
#   usr/sbin 的符号链接 —— 拼 rootfs 时它们会撞：
#
#       cp: cannot overwrite non-directory '/rootfs/./sbin'
#           with directory '.../iproute2-7.2.0-1/payload/./sbin'
#
#   （驱动里已经为 `libffi` / `/usr/lib64` 那一类在默认值里钉过 LIBDIR，
#     理由写得很清楚：**这是一类问题，不是它一个包的问题**。
#     而 SBINDIR 是同一个形状的另一个变量 —— 这里就地钉住。）
SBINDIR=/usr/sbin

build() {
    cd "$SRCDIR"

    # 有 configure 就跑。它就是个 sh 脚本。
    if [ -x ./configure ]; then ./configure; elif [ -f ./configure ]; then sh ./configure; fi

    make SBINDIR="$SBINDIR"
}

package() {
    cd "$SRCDIR"
    make DESTDIR="$PKGDEST" SBINDIR="$SBINDIR" install

    # ★ 验的是【我们的布局】：ip 和 ss 都该在 /usr/sbin。
    #
    #   上一版查的是 usr/sbin/ip、usr/bin/ss、usr/sbin/ss 三个里的任意一个 ——
    #   宽松到"有一个就算过"。而当时实际是三个都不在，所以它还是报了错，
    #   这一点它是做对了的（那个报错精准地指出了成因）。
    #
    #   现在既然钉死了 SBINDIR，就直接要求两个都在，并且【不许】出现真目录 sbin。
    for f in usr/sbin/ip usr/sbin/ss; do
        [ -x "$PKGDEST/$f" ] || { echo "  ✗ $f 不在"; exit 1; }
    done
    echo "  ✓ ip 和 ss 都在 /usr/sbin"

    # 真目录 sbin 是那个 bug 的指纹。它回来了就说明 SBINDIR 没传进去。
    [ -d "$PKGDEST/sbin" ] && {
        echo "  ✗ 包里出现了真目录 sbin —— SBINDIR 没生效："
        ls -la "$PKGDEST/sbin" | head -5 | sed 's/^/      /' || true
        exit 1
    }
    echo "  ✓ 包里没有真目录 sbin"
}
