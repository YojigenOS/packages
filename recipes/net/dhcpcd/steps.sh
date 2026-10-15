# dhcpcd 的构建步骤
#
# 上游的 configure/make 就够用，这里只多一件事：把首启钩子装上。

package() {
    # ★ 重定义 package() 就等于把默认那一步接管过来，所以这行不能少。
    #   驱动给 autotools 的默认步骤就是这一条（build.py 里 autotools 那道默认步骤）。
    make DESTDIR="$PKGDEST" install

    # 首启钩子。放在这里而不是 yojigen-os-init 里——**它是 dhcpcd 的功能**，
    # 谁提供能力，谁带来"要用这个能力"的那一步。
    install -Dm755 "$PKGDIR/files/15-network" \
        "$PKGDEST/usr/lib/yojigen-os/boot.d/15-network"
}
