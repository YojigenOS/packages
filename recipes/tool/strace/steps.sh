# strace 的构建步骤。
#
# 上游 INSTALL-git.md 说得很清楚（从 git 建时）：
#
#     From the git repository root:
#         ./bootstrap
#     This generates the configure script and build system files.
#
# 所以三步：bootstrap → configure → make。
#
# ★ bootstrap 要 gawk / autoconf / automake —— 都在这个配方的 build 依赖里。
#   少了它们它会以各种奇怪的方式断，而报错不会说"你缺 gawk"。

build() {
    cd "$SRCDIR"

    # 1. 上游自己那一步。它不是 autoreconf —— 是一串 gen_*.sh，
    #    从模板生成 Makefile.am 之类（tarball 里那些是现成的）。
    ./bootstrap

    # 2. 确认它真的产出了要的东西。
    #    不验的话，失败会延后到 configure，报一句和成因隔着好几层的错。
    for f in configure Makefile.in src/Makefile.in; do
        [ -e "$f" ] || { echo "  ✗ bootstrap 之后 $f 还是不在"; exit 1; }
    done
    echo "  ✓ bootstrap 生成了 configure / Makefile.in"

    # 3. 正常那两步。
    ./configure \
        --prefix=/usr \
        --enable-mpers=no \
        --disable-static \
        --disable-nls
    make
}

package() {
    cd "$SRCDIR"
    make DESTDIR="$PKGDEST" install

    [ -x "$PKGDEST/usr/bin/strace" ] || { echo "  ✗ /usr/bin/strace 不在"; exit 1; }
    echo "  ✓ strace 装好了"
}
