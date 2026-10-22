#!/bin/sh
# zip 3.0 的构建步骤。
#
# ── 上游给了什么 ─────────────────────────────────────────────────────────
#
# 顶层**没有** configure / configure.ac / Makefile —— 一切在 `unix/Makefile`
# 里，靠平台目标（`generic` / `linux` / …）选。`unix/configure` 是它自己写的
# 纯 sh 探测脚本（不是 autoconf）。
#
# ── 它为什么把"系统有这个函数"判成"没有" ──────────────────────────────────
#
# 那个脚本里的探测长这样（**八处都是**，不只 memset）：
#
#     echo "int main(){ char k; memset(&k,0,0); return 0; }" > conftest.c
#     $CC -o conftest conftest.c >/dev/null 2>/dev/null      ← 没有 $CFLAGS
#     [ $? -ne 0 ] && CFLAGS="${CFLAGS} -DZMEM"
#
# ★★ 两个问题叠在一起：
#
#   1. 那些测试程序**不带 `<string.h>`** —— 1999 年的 C 里只是隐式声明（警告），
#      而现代 gcc 当**硬错误**。
#   2. 那八行**都不带 `$CFLAGS`**（脚本里另 12 条写的是 `$CC $CFLAGS -c`）。
#
#   于是它按 gcc 的默认标准编、编不过、判"系统没有 memset"、自己加 `-DZMEM`，
#   而 `zip.h` 的 `#ifdef ZMEM` 就声明了 1999 年签名的 memset/memcpy/memcmp ——
#   撞现代 glibc：`conflicting types for 'memset'`。
#
#   **它误判我们缺 memset。我们并不缺。**
#
# ── 修法：把旗子塞进 CC ──────────────────────────────────────────────────
#
# `CC=${1-cc}` **没有引号**，所以 word split 会把它拆开 —— 塞进 CC 的旗子会进
# 每一处探测（包括那八处不带 CFLAGS 的）：
#
#     sh unix/configure "gcc -std=gnu89" "-I. -DUNIX" ""
#
# ★ 为什么不用 `CC="gcc -std=gnu89" make …`（那样会污染所有编译和链接）：
#   探测只需要"按老标准能过"，而**真正的编译**可以按现代标准来。所以这里
#   单独调一次 configure，把生成好的 `flags` 交给 make。
#
# ★ `unix/Makefile` 的 `flags:` 目标是 `sh unix/configure "${CC}" "${CFLAGS_NOOPT}"`。
#   我们绕开它、自己调 —— 因为那个目标没法给 CC 加旗子。

# ── 修法：让探测按 1999 年的"诊断级别"跑 ─────────────────────────────────
#
# 上面说了那两个问题。而 `-std=gnu89` **只解决第一个**：在那个标准下隐式声明
# 本该只是警告 —— 但**现代 gcc 即使给了 `-std=gnu89` 仍然把它当错误**
# （实测：flags 里照样出现 -DZMEM）。
#
# ★★ 所以真正管用的是把那条诊断**降级**：
#
#       -Wno-implicit-function-declaration
#       -Wno-error=implicit-function-declaration
#
#   加上之后，那个不带 `<string.h>` 的测试程序能编过 → 探测判"系统有 memset"
#   → **不会**加 -DZMEM → zip.h 里那三行老声明不会被激活 → 不撞 glibc。
#
# ★ 为什么不是 `-w`（全静默）：那也管用（实测 flags 里 ZMEM 也是 0），但它会把
#   我们自己关心的警告一起吞掉。**只降级那一条诊断**，其余照常。
#
# ★ 而旗子要放进 CC —— `CC=${1-cc}` 没引号，word split 会拆开，于是那八处不带
#   `$CFLAGS` 的探测也能看见它。

build() {
  sh unix/configure "$(command -v gcc) -std=gnu89 -Wno-implicit-function-declaration -Wno-error=implicit-function-declaration" "-I. -DUNIX" "" || true
  make -f unix/Makefile generic
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
