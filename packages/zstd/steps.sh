# zstd 的构建步骤 —— 只有一个 Makefile
#
# 顶层没有 configure，也没有 CMakeLists。`make` 会依次编 lib、programs、
# 文档之类；`make install` 需要 PREFIX 和 DESTDIR。

build() {
  cd "$SRCDIR"
  # 并行度由驱动的 MAKEFLAGS=-jN 带进来，这里不用显式传。
  make
}

package() {
  cd "$SRCDIR"
  make DESTDIR="$PKGDEST" PREFIX=/usr install
}
