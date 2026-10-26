# mailcap —— 纯数据包
#
# ★ 只有 /etc/mailcap 一个文件。
#   `$PKGDIR/files/mailcap` 是本配方目录里的那份。

build() {
    # 纯数据包，没有东西要编译
    :
}

package() {
    install -Dm644 "$PKGDIR/files/mailcap" "$PKGDEST/etc/mailcap"
}
