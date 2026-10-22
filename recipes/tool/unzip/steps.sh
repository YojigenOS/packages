#!/bin/sh
# unzip 6.0 的构建步骤。
#
# ★ 三个必须的开关（都是这套 1990 年代代码在新系统上的老问题）：
#
#   -DNO_LCHMOD      unzip 用了 lchmod()，而它在现代 glibc 上没有（BSD 才有）。
#                    不给这个，`unzip.c` 直接编不过。
#   -DLARGE_FILE_SUPPORT / -DZIP64_SUPPORT
#                    大文件和 zip64 支持，默认是关的。
#   -DUNICODE_SUPPORT
#                    中文文件名（Windows 打的 zip）要靠它 —— 而"中文优先"是
#                    这个发行版的定位，不能在这条上省。
#
# ★ 而 `LOCFLAGS` 是那个 Makefile 认的口子（拼进 CFLAGS）。

build() {
  make -f unix/Makefile \
    LOCFLAGS="-DNO_LCHMOD -DLARGE_FILE_SUPPORT -DZIP64_SUPPORT -DUNICODE_SUPPORT" \
    linux
}

package() {
  install -Dm755 unzip "$PKGDEST/usr/bin/unzip"
  # 其余几个小工具和手册页
  for t in zipinfo funzip unzipsfx; do
    [ -f "$t" ] && install -Dm755 "$t" "$PKGDEST/usr/bin/$t"
  done
  for m in unzip.1 zipinfo.1 funzip.1; do
    [ -f "man/$m" ] && install -Dm644 "man/$m" "$PKGDEST/usr/share/man/man1/$m"
  done
  return 0
}
