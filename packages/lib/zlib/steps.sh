# zlib 的构建步骤
#
# [build] 声明为 type = "custom"，所以这两步完全由这里提供。
# 驱动会先定义同名的默认函数，再 source 本文件——于是这里的定义覆盖默认值。

build() {
  ./configure --prefix=/usr
  make
}

package() {
  make DESTDIR="$PKGDEST" install
}
