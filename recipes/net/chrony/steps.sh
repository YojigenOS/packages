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

    # ── /etc/chrony.conf ────────────────────────────────────────────────
    #
    # ★ 上游【故意不装】它 —— Makefile.in 里既没有 chrony.conf 也没有 examples。
    #   那是发行版的决定，所以这里由我们定。
    #
    #   它有【三个】样例，而我第一版写的是 `chrony.conf.example`（没编号）：
    #
    #       example1   376 字节   pool.ntp.org + driftfile + makestep + rtcsync
    #       example2  1537 字节   上面再加 ntsdumpdir 和 logdir
    #       example3 13956 字节   全注释的参考手册（每行以 ! 开头）
    #
    #   取 example1：它小而完整 —— 有能用的 NTP 源（pool.ntp.org），
    #   有 driftfile（不然每次重启都从零学起），有 step（时钟差得远时能一次跳过去）。
    #   ★ 那是发行版该装的那一份，不是最全的那一份。
    if [ -f examples/chrony.conf.example1 ]; then
        install -Dm644 examples/chrony.conf.example1 "$PKGDEST/etc/chrony.conf"
        echo "  ✓ /etc/chrony.conf 用的是上游 example1（带 pool.ntp.org）"
    else
        echo "  ✗ examples/chrony.conf.example1 不在 —— 上游可能改过名字"
        ls examples/ 2>/dev/null | sed 's/^/      /'
        exit 1
    fi

    # ── /etc/logrotate.d/chrony ────────────────────────────────────────
    #
    # ★ 上游把这份放出来了，而它的存在让两个包【真的配上】：
    #   chrony 写 /var/log/chrony/，logrotate 负责不让它长满。
    #   没有这份的话，那个目录会一直长 —— 而我们把 logrotate 装上了，
    #   就该让它们接上。
    if [ -f examples/chrony.logrotate ]; then
        install -Dm644 examples/chrony.logrotate "$PKGDEST/etc/logrotate.d/chrony"
        echo "  ✓ /etc/logrotate.d/chrony 也装了（和 logrotate 那个包接上）"
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
