# linux-headers 的构建步骤
#
# headers_install 不需要先编译内核——它只从源码树里挑选并整理
# 用户空间可见的头文件，装进 $INSTALL_HDR_PATH/include。
#
# 注意 INSTALL_HDR_PATH 是完整路径，不是 DESTDIR 的相对根；
# 内核的这套变量跟 autotools 的约定不一样。

build() {
  : # 无需单独编译
}

package() {
  make headers_install INSTALL_HDR_PATH="$PKGDEST/usr"
}
