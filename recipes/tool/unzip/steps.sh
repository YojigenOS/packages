#!/bin/sh
# unzip 6.0 的构建步骤。
#
# ★ 和 zip 同一个病、同一个修法：`unix/configure` 的探测程序不带 <string.h>，
#   现代 gcc 把隐式声明当硬错误 → 它误判"系统没有这些函数"。
#   而那个开关也必须走 `LOCAL_UNZIP`（它会进 CFLAGS_NOOPT、进而进探测）。
#
# ★ 三个额外的 -D：
#   -DNO_LCHMOD      unzip 用了 lchmod()，只有 BSD 有，现代 glibc 没有
#   -DLARGE_FILE_SUPPORT -DZIP64_SUPPORT   大文件和 zip64，默认关着
#   -DUNICODE_SUPPORT Windows 打的 zip 里中文名全靠它 —— "中文优先"不能在这省

build() {
  make -f unix/Makefile \
    LOCAL_UNZIP="-std=gnu89 -DNO_LCHMOD -DLARGE_FILE_SUPPORT -DZIP64_SUPPORT -DUNICODE_SUPPORT" \
    linux
}

package() {
  for t in unzip zipinfo funzip unzipsfx; do
    [ -f "$t" ] && install -Dm755 "$t" "$PKGDEST/usr/bin/$t"
  done
  for m in unzip.1 zipinfo.1 funzip.1; do
    [ -f "man/$m" ] && install -Dm644 "man/$m" "$PKGDEST/usr/share/man/man1/$m"
  done
  return 0
}
