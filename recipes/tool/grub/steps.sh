# grub 的构建步骤
#
# 每个目标平台一个独立的 configure + make（理由见 spec.toml）。GRUB 支持
# out-of-tree 构建，所以每个目标有自己的目录，互不干扰。
#
# 可用的环境变量（由驱动注入）：$PKGDEST $PKGDIR $SRCDIR $WORK

# 本架构要建哪些目标，格式 <configure --target>:<--with-platform>
grub_targets() {
    case "$(uname -m)" in
        x86_64)
            # i386-pc   = BIOS 引导（腾讯云那台就是它）
            # x86_64-efi = UEFI 引导（云上的另一大半）
            echo "i386:pc x86_64:efi"
            ;;
        aarch64|arm64)
            # ★ 两种写法都认：Linux 的 uname 是 aarch64，macOS 是 arm64。
            #   配方在 Linux 容器里跑，所以正常路径走的是 aarch64；写别名是为了
            #   让宿主侧单独试跑这个函数时也有意义（今天就在这儿绊了一下）。
            echo "aarch64:efi"
            ;;
        *)
            echo "不认识的架构: $(uname -m)" >&2
            return 1
            ;;
    esac
}

# 目标 → /usr/lib/grub/ 下的目录名。GRUB 自己的命名规则就是
# <target>-<platform>，只有 i386:pc 是特例（它叫 i386-pc，不是 i386-pc）。
grub_moddir() {
    if [ "$1" = "i386" ]; then echo "i386-pc"; else echo "$1-$2"; fi
}

build() {
    # ── 摘掉构建环境 CFLAGS 里的 -fcf-protection ─────────────────────────
    #
    # 它是 x86_64 的 CET 加固特性；而 i386-pc 的模块是【32 位目标】，cc1 直接
    # 拒绝（实测，就是它挡住了第一次构建）：

    #     cc1: error: '-fcf-protection' is not compatible with this target
    #     make[3]: *** [Makefile:48701: trig_module-trigtables.o] Error 1
    #
    # ★ 这不是"缺 multilib"。32 位的模块编译一路都是好的——GRUB 是
    #   freestanding 的（不链 libc），所以根本不需要 32 位 glibc。真正的问题
    #   是【我们构建环境的 CFLAGS 里有一条只对 x86_64 成立的标志】。
    #
    #   所以只摘这一条，其余加固标志（-fstack-protector-strong、FORTIFY）留着
    #   ——grub-install 那些工具是要发出去的。
    CFLAGS=$(echo "${CFLAGS:-}" | sed 's/-fcf-protection[^ ]*//g')
    export CFLAGS

    mkdir -p "$WORK/build"
    for t in $(grub_targets); do
        tgt="${t%%:*}"; plat="${t##*:}"
        d="$WORK/build/${tgt}-${plat}"
        mkdir -p "$d"
        cd "$d"
        echo "── 配置 ${tgt}-${plat} ──"
        "$SRCDIR/configure" \
            --target="$tgt" \
            --with-platform="$plat" \
            --prefix=/usr \
            --disable-nls \
            --disable-werror
        make
    done
}

package() {
    for t in $(grub_targets); do
        tgt="${t%%:*}"; plat="${t##*:}"
        cd "$WORK/build/${tgt}-${plat}"
        make DESTDIR="$PKGDEST" install
    done

    # ── 自检：模块和工具真的到位了吗 ────────────────────────────────────
    #
    # 这个包的价值全在 /usr/lib/grub/<目标>/ 那一堆模块上：少了它们，
    # grub-install 写出来的引导记录是坏的——而且要到【装机之后、重启之后】
    # 才发现。所以在这里就断言，宁可构建失败。
    for t in $(grub_targets); do
        tgt="${t%%:*}"; plat="${t##*:}"
        dir=$(grub_moddir "$tgt" "$plat")
        [ -d "$PKGDEST/usr/lib/grub/$dir" ] || {
            echo "grub: 缺少模块目录 /usr/lib/grub/$dir" >&2
            return 1
        }
        n=$(ls "$PKGDEST/usr/lib/grub/$dir"/*.mod 2>/dev/null | wc -l)
        echo "  $dir: $n 个模块"
        [ "$n" -gt 50 ] || {
            echo "grub: $dir 只有 $n 个模块，不像一份完整的 GRUB" >&2
            return 1
        }
    done

    for b in grub-install grub-mkimage grub-mkstandalone grub-mkconfig; do
        [ -x "$PKGDEST/usr/sbin/$b" ] || [ -x "$PKGDEST/usr/bin/$b" ] || {
            echo "grub: 缺少 $b" >&2
            return 1
        }
    done
    echo "  grub-install / grub-mkimage / grub-mkstandalone / grub-mkconfig 都在"
}
