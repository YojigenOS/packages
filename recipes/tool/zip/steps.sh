#!/bin/sh
# zip 3.0 的构建步骤。
#
# ── 上游给了什么 ─────────────────────────────────────────────────────────
#
# 顶层**没有** configure / configure.ac / Makefile —— 一切在 `unix/Makefile`
# 里，靠平台目标（`generic` / `linux` / …）选。`unix/configure` 是它自己写的
# 纯 sh 探测脚本（不是 autoconf）。
#
# ── 为什么探测必须按 1999 年的语义跑 ──────────────────────────────────────
#
# 它判 memset 是这样（unix/configure）：
#
#     echo "int main(){ char k; memset(&k,0,0); return 0; }" > conftest.c
#     $CC -o conftest conftest.c
#     [ $? -ne 0 ] && CFLAGS="${CFLAGS} -DZMEM"
#
# ★ 那个测试程序**没有 `#include <string.h>`**。1999 年的 C 里没问题（隐式声明
#   只是警告）；现代 gcc（C23 默认）把它当**硬错误**。于是探测"失败"，它自己
#   加 `-DZMEM`，而 `zip.h` 的 `#ifdef ZMEM` 就会声明老签名的 memset/memcpy/
#   memcmp —— 撞现代 glibc：`conflicting types for 'memset'`。
#
#   **它误判我们缺 memset。我们并不缺。**
#
# ── 而那个开关必须走 LOCAL_ZIP，不是 CFLAGS ──────────────────────────────
#
# `unix/Makefile` 里：
#
#     CFLAGS_NOOPT = -I. -DUNIX $(LOCAL_ZIP)
#     CFLAGS       = -O2 $(CFLAGS_NOOPT)
#     flags: unix/configure
#             sh unix/configure "${CC}" "${CFLAGS_NOOPT}" "${IZ_BZIP2}"
#
# ★★ 它把 **CFLAGS_NOOPT** 传给探测，不是 CFLAGS。所以第一次改的时候
#    往 CFLAGS 里塞 `-std=gnu89`：**编译用了、探测没用**，报错原样还在。
#    而 `LOCAL_ZIP` 是它留的口子 —— 它会进 CFLAGS_NOOPT，也就进了探测。
#
#     LOCAL_ZIP="-std=gnu89"  →  -I. -DUNIX -std=gnu89  →  unix/configure

build() {
  make -f unix/Makefile \
    LOCAL_ZIP="-std=gnu89" \
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
