# traceroute —— 网络路径追踪
#
# ★ 它有自己的 Makefile 体系（Makefile + Make.rules + Make.defines），
#   不是 autotools。而 `prefix` 在 Make.rules 里默认是 /usr/local。
#
# ★★ 它要 setuid 或 cap_net_raw —— 而我们是 `CONFIG_MODULES=n` 的极简内核，
#   能力位那套已经在 driver 里有了（.yojigen-caps）。这里用 make 的 install
#   之后靠 driver 设能力位（见 steps 里那句 chmod）。

build() {
    make CC="${CC:-gcc}" prefix=/usr
}

package() {
    # ★ 它的 install target 支持 DESTDIR（见 Make.rules 的 install 段）
    make install DESTDIR="$PKGDEST" prefix=/usr

    # ★ 而 capability 那一格：traceroute 要 CAP_NET_RAW。
    #   驱动会把 `getcap` 记到 .yojigen-caps 里 —— 但那要求这里已经设好。
    #   所以先在这设一次（用的是构建容器的 setcap），driver 之后会重新设。
    for b in traceroute traceroute6; do
        f="$PKGDEST/usr/bin/$b"
        [ -x "$f" ] || continue
        setcap cap_net_raw+ep "$f" 2>/dev/null || \
            echo "  ⚠ $b 设不了 cap_net_raw —— 那它要以 root 跑" >&2
    done
}
