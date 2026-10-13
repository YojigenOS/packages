# procps 的构建步骤。
#
# ★ 只重定义 build()，因为默认那个会在 po/ 上断掉。

build() {
    # 驱动已经跑过 autoreconf -fi 了（配方的 autoreconf = true），
    # 所以这里直接 configure。
    ./configure \
        --prefix=/usr --sysconfdir=/etc \
        --with-systemd=no --without-watch8bit --disable-static --disable-nls

    # ★ 从 SUBDIRS 里拿掉 po。
    #
    #   先说清楚：**configure 那一大步全是对的**。日志为证：
    #
    #       Copying file po/Makefile.in.in            ← autopoint 生成了它
    #       checking whether NLS is requested... no   ← --disable-nls 生效了
    #       config.status: creating po/Makefile.in    ← 也生成了
    #
    #   断在 procps 自己的 Makefile.am 上：
    #
    #       SUBDIRS = local po-man po testsuite
    #
    #   `po` 是【无条件】列着的，而 NLS 一关，gettext 生成的那份 po/Makefile
    #   里就没有 all 目标了。于是顶层 make 走进去：
    #
    #       Making all in po
    #       make[2]: *** No rule to make target 'all'.  Stop.
    #
    #   我们不要翻译（--disable-nls 就是这个意思），所以把 po 从 SUBDIRS
    #   摘掉。**这是"少编一个目录"，不是给上游打补丁。**
    #
    #   摘完验一下：没摘干净的话，这一步应该【响亮地失败】，而不是让同一个
    #   错在 make 那里再出现一次——那时它离成因已经隔了好几层。
    sed -i '/^SUBDIRS = /s/ po / /' Makefile
    if grep -qE '^SUBDIRS = .*\bpo\b' Makefile; then
        echo "  ✗ po 还在 SUBDIRS 里（sed 没匹配上）。现在的值："
        grep '^SUBDIRS' Makefile | sed 's/^/      /'
        exit 1
    fi
    echo "  ✓ SUBDIRS 里的 po 摘掉了：$(grep '^SUBDIRS' Makefile | head -1)"

    make
}
