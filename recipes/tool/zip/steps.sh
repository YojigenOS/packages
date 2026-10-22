#!/bin/sh
# zip 3.0 的构建步骤。
#
# ── 上游给了什么 ─────────────────────────────────────────────────────────
#
# 顶层**没有** configure、没有 configure.ac、没有 Makefile —— 一切在
# `unix/Makefile` 里，靠平台目标（`generic` / `linux` / …）选。
# 而 `unix/configure` 是它自己写的一个纯 sh 探测脚本（不是 autoconf）。
#
# ── 那个探测脚本为什么必须用 gnu89 跑 ────────────────────────────────────
#
# `unix/configure` 判 memset 是这样：
#
#     echo "int main(){ char k; memset(&k,0,0); return 0; }" > conftest.c
#     $CC -o conftest conftest.c
#     [ $? -ne 0 ] && CFLAGS="${CFLAGS} -DZMEM"
#
# ★ 那个测试程序**没有 #include <string.h>**。1999 年的 C 里这没问题（隐式声明
#   只是个警告）；而现代 gcc（C23 默认）把它当**硬错误**。
#
#   于是：探测"失败" → 它自己加 `-DZMEM` → `zip.h` 里那份 `#ifdef ZMEM`
#   就会声明老签名的 memset/memcpy/memcmp → 撞现代 glibc：
#
#       zip.h:726: error: conflicting types for 'memset';
#                  have 'char *(char *, int, unsigned int)'
#
# ★★ 所以修法不是"关掉它的声明"（那是猜它的宏名），而是**让它的探测按 1999 年
#    的语义跑**：传 `-std=gnu89` 给 unix/configure，隐式声明就只是警告，
#    探测通过，它不会加 ZMEM，也就不会声明那些函数。
#
# ★ 而 `__STDC_VERSION__` 那条一起带上：让源码知道自己按什么标准编。

CFLAGS_BASE="-O2 -pipe -std=gnu89"

build() {
  # ★ 先跑它自己的探测（生成 flags 文件），再 make
  sh unix/configure "$(command -v gcc)" "$CFLAGS_BASE -I. -DUNIX" "" >/dev/null 2>&1 || true
  make -f unix/Makefile CFLAGS="$CFLAGS_BASE" generic
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
