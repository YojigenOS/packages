# which 的构建步骤。
#
# ★ 它 2015 年之后没再动过，而它的 configure 脚本是那个年代生成的 ——
#   所以【构建期】可能撞上新工具链的默认值（比如 C23 对老写法的更严要求）。
#   真撞上了就在 build() 里显式降标准，理由写清楚。

package() {
    cd "$SRCDIR"
    make DESTDIR="$PKGDEST" install

    # ★ 检查它落在哪儿。这一条是对着"前缀被顶掉"那类问题写的 ——
    #   我们已经两次见过包里的东西跑到 /usr/local 去。
    for f in usr/bin/which usr/share/man/man1/which.1; do
        [ -e "$PKGDEST/$f" ] || { echo "  ✗ $f 不在"; exit 1; }
    done
    if [ -d "$PKGDEST/usr/local" ]; then
        echo "  ✗ 有东西装进了 /usr/local —— 前缀错了"
        find "$PKGDEST/usr/local" -type f | head -5 | sed 's/^/      /' || true
        exit 1
    fi
    echo "  ✓ usr/bin/which 和它的 man 页都在，而且没有东西跑到 /usr/local"
}
