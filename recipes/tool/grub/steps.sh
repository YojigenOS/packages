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
    # ── 把我们构建环境的 CFLAGS 整条挡在门外 ─────────────────────────────
    #
    # 这一条踩了两次，两次报错都不是"缺依赖"，而是同一个病因：
    # 构建环境的 CFLAGS 是【宿主】的口味，而 GRUB 的模块是 freestanding 的
    # 目标代码——它自己那套标志才是对的。而且我们的 CFLAGS 排在后面，会
    # 覆盖掉 GRUB 自己的选择。
    #
    # 第一次：-fcf-protection（x86_64 的 CET 加固）。32 位目标直接拒绝：
    #
    #     cc1: error: '-fcf-protection' is not compatible with this target
    #
    # 第二次（只摘掉上面那一条之后）：-fstack-protector-strong 活了下来，
    # 而它排在 GRUB 的 -fno-stack-protector 后面，于是把后者覆盖了：
    #
    #     ... -fno-stack-protector -Wtrampolines ... -fstack-protector-strong ...
    #     undefined reference to `__stack_chk_fail'
    #
    # 那个符号在 -nostdlib 的链接里根本不存在。所以不是"再摘一条"，而是
    # 【整条 CFLAGS 都不该进来】。
    #
    # ★ LDFLAGS 留着：它不参与目标链接的符号解析（-z relro -z now 在两种
    #   链接里都成立），而 grub-install 那些要发出去的工具还需要它。
    unset CFLAGS

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
