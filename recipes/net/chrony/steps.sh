# chrony 的构建步骤。
#
# ★ 它是【手写 configure】的项目，不是 autotools —— 走 custom，每一步都明说。

build() {
    cd "$SRCDIR"

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

    # ★★ 不跑 `make install` —— 它最后一步是 `make -C doc install`，而那个要
    #   asciidoctor（Ruby 的一个 gem），实测：
    #
    #       asciidoctor -b manpage -o chrony.conf.man.in chrony.conf.adoc
    #       make[1]: asciidoctor: No such file or directory
    #
    #   而看它的 install 目标，二进制之后【只剩文档】这一步：
    #
    #       install: chronyd chronyc
    #           … mkdir / cp chronyd $(SBINDIR)/chronyd / cp chronyc $(BINDIR)/chronyc
    #           $(MAKE) -C doc install          ← 就这一行是文档
    #
    #   所以自己把那几行做了。**为了一个 man 页去装一整套 Ruby 不划算**，
    #   而少 man 页的代价是明确的（`man chrony` 看不到东西），不是暗的。
    #
    #   ★ 注意上游自己在 Makefile 里写了一句"不用 install 命令，因为它的开关
    #     各家不一样"—— 所以下面这几行就是【那段本身】，不是绕过它。
    make chronyd chronyc

    install -d "$PKGDEST/usr/sbin" "$PKGDEST/usr/bin" \
               "$PKGDEST/etc" "$PKGDEST/var/lib/chrony"

    install -m755 chronyd "$PKGDEST/usr/sbin/chronyd"
    # ★ chronyc 在 /usr/bin，不是 /usr/sbin —— 我上一版把检查写成了
    #   usr/sbin/chronyc，而 Makefile 里它走的是 $(BINDIR)。
    #   实测日志里那两行 cp 写得很清楚。
    install -m755 chronyc "$PKGDEST/usr/bin/chronyc"

    # 配置样例 —— 上游放在 examples/ 下，装成 /etc/chrony.conf。
    # 没有它 chronyd 起不来（那才是"装上能用"的一部分）。
    if [ -f examples/chrony.conf.example ]; then
        install -Dm644 examples/chrony.conf.example "$PKGDEST/etc/chrony.conf"
        echo "  ✓ /etc/chrony.conf 用的是上游那份样例"
    fi

    # 而 man 页能建就建（asciidoctor 在的时候），建不了就明说跳过了——
    # 不静默。
    if command -v asciidoctor >/dev/null 2>&1; then
        make -C doc install DESTDIR="$PKGDEST" && echo "  ✓ man 页也装了"
    else
        echo "  · 跳过 man 页（asciidoctor 不在——那是 Ruby 的一个 gem）"
    fi

    for f in usr/sbin/chronyd usr/bin/chronyc etc/chrony.conf; do
        [ -e "$PKGDEST/$f" ] || { echo "  ✗ $f 不在"; exit 1; }
    done
    echo "  ✓ chronyd(8) / chronyc(1) / chrony.conf 都在"
}
