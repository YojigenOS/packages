#!/bin/sh
# unzip 6.0 的构建步骤。
#
# ★ 和 zip 一样：顶层没有 configure/Makefile，只有 `unix/Makefile`。
#   而 `unix/configure` 是它自己的 sh 探测脚本，用 `$1=CC $2=CFLAGS` 调。
#
# ★★ 同样必须 `-std=gnu89` 跑那份探测 —— 理由和 zip 那边一模一样：它判函数
#    在不在时用的测试程序不带 `<string.h>`，而现代 gcc 把隐式声明当硬错误，
#    于是它会把"系统明明有"的函数判成"没有"。
#
# ★ 三个额外的 -D（zip 那边没有的）：
#   -DNO_LCHMOD      unzip 用了 lchmod()，而它只有 BSD 有，现代 glibc 没有。
#   -DLARGE_FILE_SUPPORT / -DZIP64_SUPPORT
#                    大文件和 zip64，默认关着。
#   -DUNICODE_SUPPORT Windows 打的 zip 里中文名全靠它 —— "中文优先"这条定位
#                    不允许在这上面省。

CFLAGS_BASE="-O2 -pipe -std=gnu89 -DNO_LCHMOD -DLARGE_FILE_SUPPORT -DZIP64_SUPPORT -DUNICODE_SUPPORT"

build() {
  sh unix/configure "$(command -v gcc)" "$CFLAGS_BASE -I. -DUNIX" "" >/dev/null 2>&1 || true
  make -f unix/Makefile CFLAGS="$CFLAGS_BASE" linux
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
