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
    # LDFLAGS 也挡掉。理由和 CFLAGS 那两条是同一类（构建环境的口味不该进
    # freestanding 的目标代码）。
    #
    # ★ 而这里要留一句更正：2026-10-16 我一度把【kernel.img 入口错位】
    #   归到这一条上（"`-z relro` 把 ELF 头放进首段"）。**那个结论站不住**，
    #   做了两个实验都否掉了它：
    #
    #       最小例子带 -z relro / -z now / separate-code      入口都不动 ✗
    #       整个 unset LDFLAGS 之后重建 grub，再建盘           还是 0x9074 ✗
    #
    #   真相在 make 那一行（见下面 build() 里的注释）：是
    #   `TARGET_IMG_BASE_LDOPT` 被 configure 挑成了 `--image-base`。
    #   所以这两处 unset 是【对的但无关】—— 留着，而别把它当那个 bug 的原因。
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
        # ★★★ 关键的一行：把 TARGET_IMG_BASE_LDOPT 从 `--image-base` 掰回 `-Ttext`。
        #
        #   症状（它只有真建一张盘、真跑 grub-install 才说话）：
        #
        #       grub-install: error: `/usr/lib/grub/i386-pc/kernel.img' is
        #           miscompiled: its start address is 0x9074 instead of 0x9000: ld.gold bug?.
        #
        #   —— 而那句 "ld.gold bug?" 是 GRUB 的【猜测】，不是结论。
        #
        #   真相分三段：
        #
        #   ① configure 里（configure.ac:1800 那段）：
        #
        #          AX_CHECK_LINK_FLAG([-Wl,--image-base,0x400000],
        #              [TARGET_IMG_BASE_LDOPT="-Wl,--image-base"],   ← 先试
        #              [TARGET_IMG_BASE_LDOPT="-Wl,-Ttext"],         ← 不行才用它
        #              [], …)
        #
        #      ★ 而 AX_CHECK_LINK_FLAG 只验"链接器【接受】这个 flag"，
        #        不验"它的语义对不对"。**现代 GNU ld 把它收下了**
        #        （只打一句 'address of `text-segment` isn't multiple of page size'），
        #        于是永远走不到那个 else。
        #
        #   ② 而 Makefile 拼出来的是（grub-core/Makefile.core.am:27384）：
        #
        #          kernel_exec_LDFLAGS = … $(TARGET_IMG_BASE_LDOPT),0x9000
        #
        #      —— 也就是 `-Wl,--image-base,0x9000`。
        #
        #   ③ 而 `--image-base` 的语义【不是】"text 从 0x9000 起"：它把 ELF 头
        #      也算进基址。实测（同一个 ld，最小例子）：
        #
        #          -Wl,--image-base,0x8000  →  Entry 0x822c   ← 0x2c 的偏移 ✗
        #          -Wl,-Ttext,0x8000        →  Entry 0x8000   ✓
        #
        #      而 GRUB 那个是 0x9074 − 0x9000 = 0x74 = ELF32 头 52 + 2×phdr 32，
        #      同一个病。
        #
        #   所以修法就是在 make 上覆盖那个变量。而它是个 make 变量
        #   （`$(TARGET_IMG_BASE_LDOPT)`），所以命令行覆盖是干净的。
        #
        #   ★ 上游也在这条路上：[PATCH 5/5] configure: drop -Ttext checks for
        #     i386-pc（grub-devel 2026-01）—— 那串补丁正是在动这个检查和这两条分支。
        #
        #   ★ 而它的代价链值得记：kernel.img 坏 → grub-install 失败 → 那张盘没有
        #     BIOS 引导代码 → 真启动时【串口一个字都没有】→ 而那要十分钟之后才看得到。
        #     见 docs/决策记录/0013 第七节。
        make TARGET_IMG_BASE_LDOPT="-Wl,-Ttext"
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
