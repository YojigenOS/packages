#!/bin/sh
# zip 3.0 的构建步骤。
#
# ★ 和 unzip 一个路子：`unix/Makefile` + 平台目标 `generic`（linux 那个目标在
#   某些版本上会去 link 没有的库，generic 最稳）。
#
# ★ 中文/Unicode 文件名：`-DUNICODE_SUPPORT`（zip 3.0 支持，要显式开）。

build() {
  make -f unix/Makefile \
    LOCFLAGS="-DUNICODE_SUPPORT" \
    generic
}

package() {
  for t in zip zipcloak zipnote zipsplit; do
    [ -f "$t" ] && install -Dm755 "$t" "$PKGDEST/usr/bin/$t"
  done
  for m in zip.1 zipcloak.1 zipnote.1 zipsplit.1; do
    [ -f "man/$m" ] && install -Dm644 "man/$m" "$PKGDEST/usr/share/man/man1/$m"
  done
  return 0
}
