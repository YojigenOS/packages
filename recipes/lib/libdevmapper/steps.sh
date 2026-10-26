# libdevmapper —— 从 LVM2 源码树只编 libdm 那一层
#
# ★ `$SRCDIR` 是 LVM2 的源码根（驱动算的），里面有 libdm/ 子目录。
#
# ★★ 只跑 `libdm.device-mapper` 和 `install_device-mapper` 两个目标 ——
#   不跑 `make`（那会连 lvm 工具一起编，依赖多得多：readline、blkid、
#   thin-provisioning-tools、udev 规则……）。
#
#   实测顶层 Makefile 第 58-61 行：
#       daemons.device-mapper: libdm.device-mapper
#       tools.device-mapper:   libdm.device-mapper
#       device-mapper:         tools.device-mapper daemons.device-mapper …
#
#   ★ 而 `install_device-mapper` 只装 device-mapper 那部分（libdm + 头文件
#     + pkg-config），不装 /sbin/lvm。
#
# ★★★ configure 还是要跑（libdm 的 Makefile.in 依赖它生成的 config.h）。
#   而 configure 有几个必须给的值，否则它会去猜/报错：
#
#       --with-confdir       /etc/lvm 的配置目录
#       --with-default-run-dir  /run/lvm
#       --with-default-system-dir  /etc/lvm
#       --enable-dmfilemapd  no   ← 那个守护进程我们不要

build() {
    ./configure \
        --prefix=/usr \
        --sbindir=/usr/bin \
        --with-confdir=/etc/lvm \
        --with-default-run-dir=/run/lvm \
        --with-default-system-dir=/etc/lvm \
        --with-default-locking-dir=/run/lvm/lock \
        --with-default-pid-dir=/run/lvm \
        --enable-dmfilemapd=no \
        --disable-readline \
        --disable-selinux

    # ★★★ 只在 libdm/ 子目录里编，目标是 `ioctl`（那是"库"那部分）。
    #
    #   我原来写的 `make libdm.device-mapper` 是错的 —— 实测它会连带编
    #   `dmsetup` 那个工具，而那个在 GCC 16 下链不上：
    #
    #       dmvdostats.c:(.text+0x220): undefined reference to `show_units'
    #       make[2]: *** [Makefile:69: dmsetup] Error 1
    #
    #   ★ 而我们**根本不需要 dmsetup** —— 只要 libdevmapper.so + 头文件。
    make -C libdm -j"${JOBS:-4}" ioctl
}

package() {
    cd "$SRCDIR"
    # ★★★ 用 `install_ioctl`（libdm/Makefile.in 第 143 行）——
    #   它依赖 `install_lib_shared`，只装共享库 + 头文件 + pkg-config。
    #
    #   而 `install_device-mapper` 会连带装 dmsetup（那个编不出来，见 build()）。
    # ★★★ 三个目标，缺一不可（2026-10-26 第二次撞）：
    #
    #   `install_ioctl` 只装共享库 —— 实测它装完 /usr/lib 里有了，而
    #   `/usr/include/libdevmapper.h` 没有。而 cryptsetup 要那个头文件。
    #
    #   libdm/Makefile.in 里它们是分开的：
    #       install_ioctl:    install_lib_shared     ← .so
    #       install_include:  $(srcdir)/libdevmapper.h  ← 头文件
    #       install_pkgconfig: libdevmapper.pc       ← .pc
    #
    #   ★ 而 `install_dynamic: install_@interface@` 那个更全的也不带 include
    #     （它只管"哪种接口的库"）。
    make -C libdm install_ioctl install_include install_pkgconfig DESTDIR="$PKGDEST"

    # ★ 断言：cryptsetup 要的三样必须都在
    for f in usr/include/libdevmapper.h usr/lib/libdevmapper.so; do
        [ -e "$PKGDEST/$f" ] || {
            echo "  ✗ libdevmapper: $f 没装上 —— cryptsetup 要它" >&2
            return 1
        }
    done
    [ -f "$PKGDEST/usr/lib/pkgconfig/devmapper.pc" ] || {
        echo "  ⚠ devmapper.pc 没有 —— cryptsetup 会退回 cc.find_library（也行）" >&2
    }
}
