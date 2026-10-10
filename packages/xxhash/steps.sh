# xxhash 的构建步骤 —— 顶层一个 Makefile
#
# 它的 Makefile 里 install 目标用 PREFIX / DESTDIR，和 zstd 一样。
# 默认会编出共享库 libxxhash.so、静态库和 xsum 命令行工具。

build() {
  cd "$SRCDIR"
  make
}

package() {
  cd "$SRCDIR"
  make DESTDIR="$PKGDEST" PREFIX=/usr install
}
