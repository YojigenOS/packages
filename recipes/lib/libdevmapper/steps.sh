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

    # ★ 只编 libdm，不编 lvm 工具
    make -j"${JOBS:-4}" libdm.device-mapper
}

package() {
    cd "$SRCDIR"
    # ★ install_device-mapper 会装 libdm.so、libdevmapper.so 链接、
    #   头文件（libdevmapper.h）、pkg-config（devmapper.pc）和 man page。
    make install_device-mapper DESTDIR="$PKGDEST"

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
