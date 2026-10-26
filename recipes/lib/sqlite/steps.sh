# sqlite —— 补 DT_SONAME（上游不给）
#
# ★★★ 为什么需要这一步（2026-10-26）：
#
#   sqlite 的 `configure` 里**一处都没提 soname**（`grep -ic soname configure`
#   → 0）。而 `Makefile.in` 第 126 行是：
#
#       LDFLAGS.libsqlite3.soname = @LDFLAGS_LIBSQLITE3_SONAME@
#
#   那个 `@...@` 是 configure 的占位符 —— **而 configure 从不为它赋值**，
#   所以它变成空，链接时不传 `-Wl,-soname`。
#
#   后果：`libsqlite3.so.3.54.1` 没有 DT_SONAME。而我们驱动按 SONAME 推导
#   `provides`（见 skirtlib/elfdeps.py），没有 SONAME 就**一条 so: 都不发**。
#   于是装 gnupg 时：
#
#       ERROR: so:libsqlite3.so (no such package):
#         required by: gnupg-2.4.9-r1[so:libsqlite3.so]
#
# ★★ 而标准发行版都自己补这个 soname（Alpine 的 sqlite APKBUILD 里有
#   `-Wl,-soname,libsqlite3.so.0`；Debian 用 dh 的默认行为）。
#   库的 ABI 版本是 `.so.0`，那不是我们编的，是上游的约定。
#
# ★ 顺带：`--disable-static` 只关静态库，共享库照建（这个包名容易误会）。

build() {
    ./configure \
        --prefix=/usr \
        --disable-static \
        --enable-threadsafe \
        --enable-fts5 \
        --enable-rtree
    make -j"${JOBS:-4}"
}

package() {
    cd "$SRCDIR"
    # ★★★ 重新链一次共享库，这次带 soname。
    #
    #   为什么不改 configure/Makefile：那是上游源码，改了下次升级就丢。
    #   而这里重新执行一次链接命令是**幂等**的 —— 目标文件已经在，只重链。
    #
    #   ★ 而 soname 用 `libsqlite3.so.0`：那是 SQLite 的 ABI 版本约定
    #     （上游说"ABI 只在 .so.0 上加，不改"），Alpine/Debian 都是这个。
    ${CC:-gcc} -o libsqlite3.so sqlite3.o -shared \
        -Wl,-soname,libsqlite3.so.0 \
        ${LDFLAGS:--Wl,-z,relro -Wl,-z,now} \
        -lpthread -ldl -lm

    make install DESTDIR="$PKGDEST"

    # ★ 断言：SONAME 必须真的写进去了。
    #   ★★ 用 grep 而不是 readelf —— 构建容器里有 readelf，但这条断言
    #      要在**产物**上验，而产物可能在宿主上（宿主没有 readelf）。
    #      直接搜 .dynstr 里的字符串更稳。
    if ! grep -aq "libsqlite3\.so\.0" "$PKGDEST/usr/lib/libsqlite3.so.3.54.1" 2>/dev/null; then
        echo "  ✗ libsqlite3.so 没有 DT_SONAME —— 那 gnupg 会找不到它" >&2
        return 1
    fi
    echo "  ✓ libsqlite3.so 带 soname（libsqlite3.so.0）" >&2
}
