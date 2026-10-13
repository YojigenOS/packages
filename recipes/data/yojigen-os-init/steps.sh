# yojigen-os-init 的构建步骤
#
# 没有编译——这个包就是一个脚本。步骤只负责把它摆到内核会去找的位置。
#
# 可用的环境变量（由驱动注入，和 filesystem 那个包一样）：
#   $PKGDEST   安装目标，等于最终根文件系统里的 /
#   $PKGDIR    本配方目录的副本，files/ 下的东西在这里
#   $SRCDIR    空的源码目录（本包无源码）
#   $WORK      容器内工作区

build() {
    : # 无需编译
}

package() {
    # 内核启动后会在根目录找 /init 并 exec 它。路径是约定，不是偏好。
    install -Dm755 "$PKGDIR/files/init" "$PKGDEST/init"

    # 首启钩子。/init 按名字顺序跑这个目录下的东西——它自己不该知道
    # 每一个钩子是谁。10- 排在 20-sshd 之前：先扩盘，再起服务。
    install -Dm755 "$PKGDIR/files/10-grow-root" \
        "$PKGDEST/usr/lib/yojigen-os/boot.d/10-grow-root"
}
