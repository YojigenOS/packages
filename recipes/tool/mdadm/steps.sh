# mdadm —— 裸 Makefile
#
# ★ `$SRCDIR` 就是源码根，不 cd。
#
# ★★ 它的 Makefile 里 `DESTDIR =`（空）而安装路径是写死的 /usr/sbin。
#   所以 install 时传 DESTDIR 就够，不需要 prefix。
#
# ★ 它默认会跑 `mdadm.static` 那个 target —— 静态链接版。我们不需要
#   （动态版就够，而且静态版让包大一圈）。默认 target 是 `everything`，
#   它包含 mdadm + mdmon + 各种符号链接。

build() {
    make CC="${CC:-gcc}" CXFLAGS="${CFLAGS:--O2}" -j"${JOBS:-4}"
}

package() {
    cd "$SRCDIR"
    make install DESTDIR="$PKGDEST"

    # ★ install 之后 mdadm 是 setuid root 的（要读 /dev/md* 的元数据）。
    #   我们的 driver 会记 .yojigen-caps，但 mdadm 那几个是 setuid 不是
    #   capability —— 而 setuid 由 driver 的 setid 清单管。
    #
    # ★ smartd 和 mdadm 的 systemd unit 在源码树里，install 会装到
    #   /usr/lib/systemd/system/（Makefile 里那条 install-systemd）。
    :
}
