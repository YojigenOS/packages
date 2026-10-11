# yojigenos-init 的构建步骤
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
}
