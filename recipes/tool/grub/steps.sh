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

# 目标 → /usr/lib/grub/ 下的目录名。
#
# ★ 规则【不是】简单的 <configure --target>-<platform>。GRUB 在 configure 里
#   先把 CPU 名字归一一次（configure.ac 开头那个 case），归一后的名字才是
#   目录名：
#
#       i[[3456]]86)  → i386     （我们传的 i386，不变）
#       aarch64*)     → arm64    ★ 我们传 aarch64，目录却是 arm64-efi
#
#   这行是实测撞出来的，不是从文档抄的：
#
#       ── 配置 aarch64-efi ──    configure --target=aarch64 --with-platform=efi
#       make / make install      ✓ 模块一个不少
#       grub: 缺少模块目录 /usr/lib/grub/aarch64-efi    ✗
#
#   make install 装到的是 /usr/lib/grub/arm64-efi。GRUB 没错，错的是这个函数：
#   它以为 --target 收的名字就是目录名，而 GRUB 的 CPU 名字和 configure 收的
#   名字不是一套。
#
# ★ 为什么 x86_64 从来没暴露：i386 和 x86_64 恰好是恒等映射，两个目标都对。
#   只有 arm64 这一格会现形——而 grub 在 aarch64 上是第一次真去建。
#   （也就是说，package() 末尾那个"模块到位了吗"的断言抓到的是我自己写错的
#     名字。它拦下的正是它该拦的那种错：一个装机之后才会炸的错。）
grub_moddir() {
    case "$1" in
        i386)    echo "i386-pc"   ;;   # 归一后仍然是 i386
        aarch64) echo "arm64-$2"  ;;   # 归一成 arm64，见上面那段
        *)       echo "$1-$2"     ;;
    esac
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
    # ★★ LDFLAGS 也【必须】挡掉 —— 2026-10-16 才发现，而这一条代价最大：
    #    它让【我们建出来的盘不能引导】。
    #
    #    原来这里留着 LDFLAGS，理由写的是"它不参与目标链接的符号解析
    #    （-z relro -z now 在两种链接里都成立）"。**那个判断是错的**，
    #    而错在一个不显眼的地方：`-z relro` 的语义就是
    #    **把 ELF 头放进第一个可加载的段**。
    #
    #    于是 i386-pc 的 kernel.img 首段从 0x9000 起，而首字节成了 ELF 头：
    #
    #        ELF32 头 52 字节 + 2 个 program header × 32 = 116 = 0x74
    #        入口 = 0x9000 + 0x74 = 0x9074   ← 而它该是 0x9000
    #
    #    而 GRUB 自己有个 sanity check，装机时才报：
    #
    #        grub-install: error: `/usr/lib/grub/i386-pc/kernel.img' is
    #            miscompiled: its start address is 0x9074 instead of 0x9000: ld.gold bug?.
    #
    #    ★ 那句"ld.gold bug?"是 GRUB 的【猜测】，它不知道是谁干的。
    #      而真相是上面的 LDFLAGS。所以查上游（grub-devel 2026-01 确实有
    #      人在 2.14 上撞到同一个报错）会把人带去错的方向。
    #
    #    ★ 而它的代价链很长：内核.img 坏 → grub-install 失败 → 那张盘没有
    #      BIOS 引导代码 → 真启动时【串口一个字都没有】→ 而那要十分钟之后
    #      才看得到。见 docs/决策记录/0013 第七节。
    unset CFLAGS
    unset CXXFLAGS
    unset LDFLAGS

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
