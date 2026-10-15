# lsof 的构建步骤。
#
# 只重定义 build()，为了补一个 soelim。

build() {
    cd "$SRCDIR"

    # ── ★ 造一个 soelim 的替身 ────────────────────────────────────────
    #
    #   实测：make 走到这一条就断
    #
    #       lsof.man: Lsof.8 version 00DIALECTS
    #               soelim < Lsof.8 > $@
    #       /bin/sh: 1: soelim: not found
    #
    #   soelim 是 groff 的一部分（groff 的 soelim 做的是"预处理 man 源"）。
    #   而 lsof 里【没有开关能跳过它】—— configure.ac 里 man 相关的
    #   AC_ARG_ENABLE/AC_ARG_WITH 一个都没有。
    #
    #   三条路：装 groff（它自己一堆依赖，为了一个 man 页不值）、
    #   跳过生成、或者**给它一个能用的替身**。选第三条。
    #
    # ★ 注意：不能简单地 `cat`。
    #
    #   soelim 在这里真正做的事是展开 `.so <file>` 指令，而 Lsof.8 里
    #   **确实有两处**（看过原文才敢这么说）：
    #
    #       .so ./version
    #       .so ./00DIALECTS
    #
    #   而那正是那条规则里另外两个先决条件 —— make 会先生成它们，
    #   然后 soelim 把内容插进 man 页。`cat` 的话这两行会原样留在
    #   输出里，man 页上会出现 ".so ./version" 而不是版本号。
    #
    #   所以替身要真的展开，而且是【读 stdin】（原命令是 `soelim < Lsof.8`）。
    #   awk 不带文件名参数时正好读 stdin。
    mkdir -p "$WORK/bin"
    cat > "$WORK/bin/soelim" <<'SHIM'
#!/bin/sh
# groff 的 soelim 的最小替身：把 ".so <file>" 展开成那个文件的内容。
# lsof 的 Lsof.8 里就两处（./version 和 ./00DIALECTS），一层就够，不需要递归。
awk '
    /^\.so[ \t]/ {
        f = $2
        while ((getline line < f) > 0) print line
        close(f)
        next
    }
    { print }
' "$@"
SHIM
    chmod +x "$WORK/bin/soelim"
    export PATH="$WORK/bin:$PATH"
    echo "  ✓ 放了一个 soelim 替身：$(command -v soelim)"

    # 验一句 —— 它真的读 stdin 且真的展开。
    # 不验的话，失败会延后到 make 里那句看不清的 127。
    printf '.so ./SENTINEL\n底下的行\n' > /tmp/_soelim_test
    echo "应该被展开的内容" > ./SENTINEL
    got=$(soelim < /tmp/_soelim_test | head -1 || true)
    rm -f /tmp/_soelim_test ./SENTINEL
    [ "$got" = "应该被展开的内容" ] || { echo "  ✗ soelim 替身没展开（拿到的是：${got}）"; exit 1; }
    echo "  ✓ 替身验过：能读 stdin、能展开 .so"

    ./configure \
        --prefix=/usr \
        --without-libtirpc

    make
}

package() {
    cd "$SRCDIR"
    make DESTDIR="$PKGDEST" install

    [ -x "$PKGDEST/usr/bin/lsof" ] || { echo "  ✗ /usr/bin/lsof 不在"; exit 1; }
    echo "  ✓ lsof 装好了"

    # man 页生成对了吗（那两处 .so 应该已经变成版本号）
    if [ -f "$PKGDEST/usr/share/man/man8/lsof.8" ]; then
        n=$(grep -c '^\.so' "$PKGDEST/usr/share/man/man8/lsof.8" 2>/dev/null || echo 0)
        [ "$n" = 0 ] && echo "  ✓ man 页里没有残留的 .so（替身真的展开了）" \
                     || echo "  · man 页里还有 $n 行 .so（不影响功能，但说明替身没完全等价）"
    fi
}
