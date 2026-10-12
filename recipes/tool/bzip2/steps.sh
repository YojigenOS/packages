# bzip2 的构建步骤
#
# 它没有 configure，只有一份老式 Makefile。三处和现代构建体系不一样的地方，
# 都在下面显式处理了（理由见 spec.toml）：
#
#   1. 不认 DESTDIR   → 把 PREFIX 直接指到目标根里
#   2. install 造硬链接（绝对路径）→ 换成相对符号链接
#   3. 默认不建共享库 → 单独 make -f Makefile-libbz2_so
#
# 可用的环境变量（由驱动注入）：$PKGDEST $PKGDIR $SRCDIR $WORK

build() {
    # ★ 顺序不能反：共享库【必须第一个建】。
    #
    #   Makefile-libbz2_so 指望那些 .o 是 -fPIC 编出来的，却和主 Makefile
    #   共用同一批 .o 文件名。先跑 `make`（不带 -fPIC）的话，它会把旧 .o
    #   当成"已最新"直接拿去链 .so，于是：
    #
    #       ld: blocksort.o: relocation R_X86_64_PC32 against symbol
    #           `stderr@@GLIBC_2.2.5' can not be used when making a shared
    #           object; recompile with -fPIC
    #
    #   ——又是只有 x86_64 会挂（aarch64 的代码本来就位置无关，碰巧能过）。
    #   先建 .so 就对了：PIC 的 .o 拿去做静态库和可执行文件都没问题，
    #   反过来不成立。
    make -f Makefile-libbz2_so

    # 静态库 + 命令行工具（复用上面那批 PIC 的 .o）
    make
}

package() {
    d="$PKGDEST"

    # ── 1. 命令行工具 + man ─────────────────────────────────────────────
    # 它的 Makefile 不认 DESTDIR，所以 PREFIX 直接写进目标根。
    # 这一步会装 bzip2、bunzip2、bzcat、bzip2recover 和几个 man。
    make install PREFIX="$d/usr"

    # ── 2. 硬链接换成符号链接 ───────────────────────────────────────────
    # `make install` 用 `ln -f $(PREFIX)/bin/bzip2 ...` 造 bunzip2/bzcat，
    # 那是【指向构建目录绝对路径的硬链接】——打进包里就是三份一样的二进制
    # （或者更糟：悬空）。各发行版都换成相对符号链接。
    for n in bunzip2 bzcat; do
        rm -f "$d/usr/bin/$n"
        ln -s bzip2 "$d/usr/bin/$n"
    done

    # 那几个 shell 脚本（bzmore/bzless/bzgrep/bzdiff/bzcmp）在这个 tarball 里
    # 有就给装上——它们不是 make install 的一部分。
    for f in bzgrep bzmore bzless bzdiff bzcmp; do
        [ -f "$f" ] && install -Dm755 "$f" "$d/usr/bin/$f"
    done

    # ── 2.5 man 页挪到位 ────────────────────────────────────────────────
    # 这份 Makefile 装到 $(PREFIX)/man，而 FHS 是 /usr/share/man。各发行版都
    # 得打这个补丁（我们不打补丁，直接挪）。不挪的后果是 man 找不到它们。
    if [ -d "$d/usr/man" ]; then
        mkdir -p "$d/usr/share/man"
        mv "$d/usr/man"/* "$d/usr/share/man"/ 2>/dev/null || true
        rmdir "$d/usr/man" 2>/dev/null || true
    fi

    # ── 3. 共享库 ───────────────────────────────────────────────────────
    # soname 是 libbz2.so.1.0（见 Makefile-libbz2_so），所以链接名是
    # libbz2.so.1.0 —— 只有它和 .so 两个名字对上，动态链接才会真的发生。
    install -Dm755 libbz2.so.1.0.8 "$d/usr/lib/libbz2.so.1.0.8"
    ln -sf libbz2.so.1.0.8 "$d/usr/lib/libbz2.so.1.0"
    ln -sf libbz2.so.1.0   "$d/usr/lib/libbz2.so"

    # ── 4. 头文件和静态库 ───────────────────────────────────────────────
    # Makefile-libbz2_so 只产出 .so，这两样要自己装（-lbz2 和 #include <bzlib.h>
    # 都得能找到东西）。
    install -Dm644 bzlib.h  "$d/usr/include/bzlib.h"
    install -Dm644 libbz2.a "$d/usr/lib/libbz2.a"
}
