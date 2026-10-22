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

build() {
  make -j"$(nproc)" \
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
