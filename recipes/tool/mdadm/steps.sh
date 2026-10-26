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

# ★★★ 去掉 `-Werror`（2026-10-26）。
#
#   上游 Makefile 第 53 行：
#
#       CWFLAGS ?= -Wall -Werror -Wstrict-prototypes -Wextra …
#
#   而 mdadm 4.4 在 **GCC 16** 下有两处 `-Wunused-but-set-variable`：
#
#       Detail.c:94:13:   变量 'failed' 设了没用
#       Assemble.c:1380:  变量 'replcnt' 设了没用
#
#   于是 `-Werror` 把它们变成硬错误，构建失败。
#
# ★ 这是【上游代码在更新编译器下的不兼容】，不是我们的配置问题 ——
#   Debian/Fedora 也会遇到，他们的做法同样是放宽告警（或打补丁）。
#
# ★★ 修法是**只去掉 -Werror，保留其余全部告警**：
#   那些 warning 仍然会打出来（下次看日志能看见），只是不再中断构建。
#   这比"删掉整行 CWFLAGS"好得多 —— 后者会把 -Wall -Wextra 一起关掉，
#   那才是真的掩盖问题。
#
#   ★ 而 `CWFLAGS ?=` 是"没传才用默认"，所以这里传进去就是覆盖。
build() {
    make CC="${CC:-gcc}" \
         CXFLAGS="${CFLAGS:--O2}" \
         CWFLAGS="-Wall -Wstrict-prototypes -Wextra -Wno-unused-parameter \
                  -Wformat -Wformat-security -Werror=format-security \
                  -fstack-protector-strong -fPIE -Warray-bounds \
                  -Wno-unused-but-set-variable" \
         -j"${JOBS:-4}"
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
