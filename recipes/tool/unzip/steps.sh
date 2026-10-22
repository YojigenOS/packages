#!/bin/sh
# unzip 6.0 的构建步骤。
#
# ★ 和 zip 同一个病、同一个修法（见 zip 那份 steps.sh 的说明）：
#   `unix/configure` 的八处探测都不带 `$CFLAGS`，而测试程序不带 `<string.h>`，
#   现代 gcc 把隐式声明当硬错误 → 它误判"系统没有这些函数"。
#
# ★ 修法：把 `-std=gnu89` 塞进 CC（`CC=${1-cc}` 没引号，word split 会拆开）。
#
# ★ 三个额外的 -D（zip 那边没有）：
#   -DNO_LCHMOD      unzip 用了 lchmod()，只有 BSD 有，现代 glibc 没有
#   -DLARGE_FILE_SUPPORT -DZIP64_SUPPORT   大文件 / zip64，默认关着
#   -DUNICODE_SUPPORT Windows 打的 zip 里中文名全靠它 —— "中文优先"不能在这省

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
  sh unix/configure \
    "$(command -v gcc) -std=gnu89 -Wno-implicit-function-declaration -Wno-error=implicit-function-declaration -DNO_LCHMOD -DLARGE_FILE_SUPPORT -DZIP64_SUPPORT -DUNICODE_SUPPORT" \
    "-I. -DUNIX" "" || true
  make -f unix/Makefile linux
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
