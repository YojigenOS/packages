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

# ★★ 必须 `-std=gnu17`（2026-10-22 撞到的）：
#
#       reflog.c:213:104: error: macro 'unreachable' passed 3 arguments,
#                              but takes just 0
#
#   我们的 gcc 默认是 C23，而 C23 的 <stddef.h> 里有一个 `unreachable()` 宏
#   （零参数）。git 自己的代码里也有一个叫 unreachable 的宏/函数、带参数 ——
#   于是被 C23 那个顶掉，整份源码编不过。
#
#   ★ 这不是我们的错，也不是 git 的错：是"编译器默认标准变了"这件事。而
#     **必须显式钉住标准**，否则同一个配方在不同的 gcc 上行为不同。
CFLAGS="-std=gnu17 -O2 -pipe"

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
