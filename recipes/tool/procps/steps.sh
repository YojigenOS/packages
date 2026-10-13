# procps 的构建步骤。
#
# ★ 只重定义 build()，因为默认那个会在 po/ 上断掉。
#   （configure 的参数也搬到这里了 —— build() 整个被重定义，写两处会不一致。）

build() {
    # 驱动已经跑过 autoreconf -fi（配方的 autoreconf = true），这里直接 configure。
    ./configure \
        --prefix=/usr \
        --sysconfdir=/etc \
        --with-systemd=no \
        --without-watch8bit \
        --disable-static \
        --disable-nls

    # ★ 从 SUBDIRS 里删掉 po。
    #
    #   先说清楚前提：**configure 那一大步全是对的**。日志为证：
    #
    #       Copying file po/Makefile.in.in            ← autopoint 生成了
    #       checking whether NLS is requested... no   ← --disable-nls 生效了
    #       config.status: creating po/Makefile.in    ← 也生成了
    #
    #   断在 procps 自己的 Makefile.am：SUBDIRS 里 `po` 是无条件列着的，
    #   而 NLS 一关，gettext 生成的那份 po/Makefile 就没有 all 目标。
    #
    # ── ★ 而 SUBDIRS 是【多行续行】的，这一点我第一版想错了 ──
    #
    #   Makefile 里它是这样：
    #
    #       SUBDIRS = \
    #       <tab>local \
    #       <tab>po-man \
    #       <tab>po \          ← po 在【自己那一行】
    #       <tab>testsuite
    #
    #   我第一版写的是行内替换（s/ po / /），只动了第一行（那行只有
    #   `SUBDIRS = \`），po 原封不动。
    #
    #   更糟的是那个检查 —— 我写的也是行内匹配：
    #
    #       grep -qE '^SUBDIRS = .*\bpo\b'
    #
    #   **它匹配不到多行里的 po，于是静默通过**，我的标记还打印了
    #   "✓ 摘掉了"（而它只打到续行符为止）。
    #
    #   ★ 一个匹配不到【真实结构】的检查，绿的和红的都不算数。
    #     这一条和"一个会失败但没人听的检查 = 没有检查"是同一类的两面。
    #
    #   所以要删的是【那一整行】：
    sed -i '/^[[:space:]]*po[[:space:]]*\\$/d' Makefile

    # 检查也按行来：有没有哪一行【就是】po（可带续行符）。
    # 这次匹配的是真实结构，不是我以为的结构。
    if grep -qE '^[[:space:]]*po[[:space:]]*\\?$' Makefile; then
        echo "  ✗ Makefile 里还有一行就是 po（sed 没删掉）。上下文："
        grep -n -B2 -A2 -E '^[[:space:]]*po[[:space:]]*\\?$' Makefile | sed 's/^/      /'
        exit 1
    fi
    echo "  ✓ SUBDIRS 里没有 po 了（它是多行的，删的是那一行）"

    make
}
