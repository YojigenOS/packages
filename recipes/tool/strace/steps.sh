# strace 的构建步骤。
#
# 上游 INSTALL-git.md 说得很清楚（从 git 建时）：
#
#     From the git repository root:
#         ./bootstrap
#     This generates the configure script and build system files.
#
# 所以三步：bootstrap → configure → make。

build() {
    cd "$SRCDIR"

    # ── 先说清楚 bootstrap 那两个问题 ──────────────────────────────────
    #
    # ① 为什么用 `sh ./bootstrap` 而不是 `./bootstrap`
    #
    #   实测报过：
    #
    #       /build/steps.sh: line 19: ./bootstrap: No such file or directory
    #
    #   而那个文件确实在（1263 字节，上游树的根目录）。带 shebang 的脚本
    #   报这句话，除了"文件真的不在"，还有几个别的原因：
    #
    #       · 没有执行位        → 那句话是 Permission denied
    #       · shebang 的解释器不在  → No such file
    #       · 行尾是 CRLF        → No such file（内核去找 "/bin/sh\r"）
    #
    #   而 `sh ./bootstrap` **绕开前两个**：不依赖执行位，也不走 shebang。
    #
    # ② 而如果绕过之后还是不行，要能一眼看出来
    #
    #   下面那句 ls 是诊断：文件在不在、什么权限、当前在哪个目录。
    #   **与其下一轮再猜，不如这一轮就把答案要出来。**
    echo "  ── 环境 ──"
    echo "    pwd = $(pwd)"
    ls -la bootstrap configure.ac Makefile.am 2>&1 | sed 's/^/    /'

    if [ ! -f ./bootstrap ]; then
        echo "  ✗ ./bootstrap 不在上面那张表里"
        echo "    当前目录的内容（前 20 项）："
        ls -la | head -20 | sed 's/^/      /'
        exit 1
    fi

    # 1. 上游自己那一步。
    sh ./bootstrap

    # 2. 确认它真的产出了要的东西。
    for f in configure Makefile.in src/Makefile.in; do
        [ -e "$f" ] || { echo "  ✗ bootstrap 之后 $f 还是不在"; exit 1; }
    done
    echo "  ✓ bootstrap 生成了 configure / Makefile.in / src/Makefile.in"

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
