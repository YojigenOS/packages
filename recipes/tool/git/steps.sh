#!/bin/sh
# git 的构建步骤 —— 它不用 autotools，用自己那套 Makefile。
#
# ★ 关掉的三样：
#   NO_ASCIIDOC / NO_DOC   —— 文档要 asciidoc + docbook，那是给人看的
#   NO_TCLTK               —— gitk/git-gui 要 Tcl/Tk
#   NO_GETTEXT             —— 翻译
#
# ★ 留着的：openssl（https）、curl（http(s) 的 remote helper）、perl（git svn
#   之类的脚本）、expat（.gitattributes 的 xml 处理不是必需，但 Debian 也开）。

# ★ CFLAGS 不再钉标准（2026-10-22 第二轮）：
#
#   2.47 在现代 gcc 上连着撞两处（C23 的 `unreachable` 宏、以及 `thread_local`
#   在 C17 里没有定义）。两处补丁都只能"治一个、冒一个"。
#   **换成 2.52 之后两处都不需要** —— 上游自己适配了 C23。
#
#   ★ 记一笔判据：补丁是最后手段。同一处连着要打第二个补丁，说明选错了版本。

build() {
  make -j"$(nproc)" \
    CFLAGS="$CFLAGS" \
    prefix=/usr \
    NO_ASCIIDOC=YesPlease \
    NO_DOC=YesPlease \
    NO_TCLTK=YesPlease \
    NO_GETTEXT=YesPlease \
    NO_INSTALL_HARDLINKS=YesPlease \
    all
}

package() {
  make -j"$(nproc)" \
    CFLAGS="$CFLAGS" \
    prefix=/usr \
    DESTDIR="$PKGDEST" \
    NO_ASCIIDOC=YesPlease \
    NO_DOC=YesPlease \
    NO_TCLTK=YesPlease \
    NO_GETTEXT=YesPlease \
    NO_INSTALL_HARDLINKS=YesPlease \
    install install-hardlinks 2>/dev/null || \
  make prefix=/usr DESTDIR="$PKGDEST" \
    NO_ASCIIDOC=YesPlease NO_DOC=YesPlease NO_TCLTK=YesPlease NO_GETTEXT=YesPlease \
    NO_INSTALL_HARDLINKS=YesPlease install
}
