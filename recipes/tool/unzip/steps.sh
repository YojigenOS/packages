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

# ── 上一版为什么还没生效（第三次才对）────────────────────────────────────
#
# 那个探测行是：
#
#     echo Check for memset
#     echo "int main(){ char k; memset(&k,0,0); return 0; }" > conftest.c
#     $CC -o conftest conftest.c >/dev/null 2>/dev/null        ← 注意这一行
#     [ $? -ne 0 ] && CFLAGS="${CFLAGS} -DZMEM"
#
# ★★ **它是整份 configure 里唯一一条不带 `$CFLAGS` 的编译**（另外 12 条都写的是
#    `$CC $CFLAGS -c conftest.c`）。所以前三次塞的旗子（宏名、CFLAGS、
#    LOCAL_ZIP）**它一次都没看见** —— 那不是传参通道的问题，是这一行的笔误。
#
# ★ 而 `CC=${1-cc}` **没有加引号**，于是 word split 会把它拆开。所以把旗子塞进
#   CC 就能进这一行：
#
#       zip generic CC="gcc -std=gnu89"       ← 不选这条路
#
#   ★ 但我们**不这么做**：CC 里带旗子会污染所有编译（包括正常编译），而且
#     `make` 里 CC 还用来链接，旗子混进去容易出别的问题。
#
# ★★★ 正路：**把那一行补上 `$CFLAGS`**（一个 patch）。上游的笔误，一行。

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
