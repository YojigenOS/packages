# linux 的构建步骤
#
# 内核的构建体系和 autotools 完全是两回事：它自己的 Kbuild 管一切，配置
# 存在 .config 里，产物在 arch/<arch>/boot/ 下。所以这里全部自己写。
#
# 可用的环境变量（由驱动注入）：
#   $PKGDEST   安装目标，等于最终根文件系统里的 /
#   $PKGDIR    本配方目录的副本，config.fragment 在这里
#   $SRCDIR    内核源码树
#   $WORK      容器内工作区
#
# 【并行度】驱动会把 MAKEFLAGS=-j<N> 作为环境变量传进来，make 自己会认，
# 所以这里不用也不能写死 -j。

build() {
    # defconfig 给一个该架构的可用默认配置。它很大（几千个驱动），但它是
    # 【已知能启动】的起点——从零手写配置是另一种难度的活。
    make defconfig

    # 再把我们真正在乎的那几项盖上去。
    #
    # -m 是"只合并，不立刻 olddefconfig"，因为要连着做。
    # merge_config.sh 会在某个选项不存在时报出来——这正是我们要的：
    # 升级内核时，消失的选项应该吵一声，而不是被静默忽略。
    scripts/kconfig/merge_config.sh -m .config "$PKGDIR/config.fragment"

    # 合并只改了值，依赖关系要重算一遍。新增的选项取默认值。
    make olddefconfig

    # 编译。文件系统镜像（Image / bzImage）是自解的，不需要额外工具。
    #
    # ★ 目标名分架构：arm64 叫 Image，x86_64 叫 bzImage。
    #
    #   这里原来写死 `make Image`——在 aarch64 上一直是对的，在 x86_64 上会
    #   报 `No rule to make target 'Image'`，而且是在【编了几十分钟之后】才报。
    #   x86_64 的内核到 2026-10-12 才第一次真去建，所以这行从写下那天起就是错的，
    #   只是没人撞上。
    case "$(uname -m)" in
        aarch64) make Image   ;;
        x86_64)  make bzImage ;;
        *) echo "不认识的架构: $(uname -m)" >&2; return 1 ;;
    esac
}

package() {
    case "$(uname -m)" in
        aarch64) IMG=arch/arm64/boot/Image   ;;
        # ★ 是 arch/x86/ 而不是 arch/x86_64/：x86 的源码树一个目录
        #   管 32/64 位，没有 arch/x86_64/ 这个目录（写错的后果是
        #   编完之后在 install 那一步找不到文件）。
        x86_64)  IMG=arch/x86/boot/bzImage    ;;
        *) echo "不认识的架构: $(uname -m)" >&2; return 1 ;;
    esac

    # 启动时内核由引导器/QEMU 直接载入，所以放在 /boot 而不是 /usr。
    # 名字统一叫 vmlinuz——run-vm.sh 和将来的引导器都只认这一个名字，
    # 不必知道底下是 Image 还是 bzImage。
    install -Dm644 "$IMG" "$PKGDEST/boot/vmlinuz"

    # 这两样是诊断用的，出问题时它们能回答"跑的是哪个内核、符号在哪"。
    #
    # ★ 用 $PKGVER 而不是 ${VERSION}。驱动传进来的是 PKGVER（见 build.py 里的
    #   `-e PKGVER=...`），$VERSION 从来不存在——第一次构建时它展开成空串，
    #   产物里就是 `config-` 和 `System.map-` 这种没版本号的名字。
    #   这件事当时没被发现，因为驱动那时还没让构建失败真的失败（见 build.py
    #   里 header 那段注释）。
    install -Dm644 System.map "$PKGDEST/boot/System.map-$PKGVER"
    install -Dm644 .config    "$PKGDEST/boot/config-$PKGVER"
}
