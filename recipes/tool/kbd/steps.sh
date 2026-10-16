# kbd 的构建步骤
#
# ★ 只有 package() —— 构建那一段走 `[build] type = "autotools"` 的默认驱动
#   （configure --prefix=/usr && make），配方里没什么要改的。
#
# ★ 而这个 package() 存在的理由【不是构建】，是那个 boot.d 钩子：
#   装了 setkeycodes 而没人调它，等于没装。

package() {
    cd "$SRCDIR"
    make DESTDIR="$PKGDEST" install

    # 自检：我们要的就是这一个。少了它整个包白建。
    [ -x "$PKGDEST/usr/bin/setkeycodes" ] || {
        echo "  ✗ /usr/bin/setkeycodes 不在 —— 这个包就是为了它"
        exit 1
    }
    echo "  ✓ setkeycodes 装好了"

    # ★ 启动钩子。理由写在 files/08-console-keyboard 里（那段是给以后的人看的）。
    #
    #   号码 08 的位置：boot.d 按名字顺序跑，现有的是
    #       05-hostname   10-grow-root   15-network   20-sshd
    #   而这个只跟控制台键盘有关，越早干净越好，所以放在 05 和 10 之间。
    install -Dm755 "$PKGDIR/files/08-console-keyboard" \
        "$PKGDEST/usr/lib/yojigen-os/boot.d/08-console-keyboard"
    echo "  ✓ 08-console-keyboard 钩子装好了"
}
