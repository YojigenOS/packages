# procps 的构建步骤。
#
# ★ 重定义 build()（默认那个会在 po/ 上断）。
#   configure 的参数也搬到这里 —— build() 整个被重定义，写两处会不一致。

build() {
    # ★ CFLAGS 里强制 include stdbool.h。
    #
    #   实测：po 那关过了之后，下一关是编译错误
    #
    #       src/watch.c:1313:47: error: 'false' undeclared
    #         note: 'false' is defined in header '<stdbool.h>';
    #               this is probably fixable by adding '#include <stdbool.h>'
    #
    #   **编译器自己给了修法。** watch.c 用了 false 却没 include 那个头——
    #   上游大概是靠别的头间接带进来的，而我们的工具链不带。
    #
    #   为什么用 -include 而不是给 watch.c 打补丁：
    #
    #     这类"少一个 include"在程序里往往不止一处，而**一轮只能撞一个**
    #     （make 停在第一个错）。一个一个打补丁 = 一轮一个错，代价是几十分钟
    #     乘以未知个数。
    #
    #     `-include` 是"每个翻译单元开头都插这一行"，一次覆盖全部。
    #     **它不是精确的手术，但它把"未知个数的回合"变成"一次"。**
    #     真需要精确时（比如某个文件因为别的理由不能带这个头）再改成补丁。
    #
    #   （步骤见 decisions/0001：这个项目里给包打补丁是常规手段，
    #     gzip 和 shadow 都有。这里选 -include 是权衡后的决定，不是不知道有补丁。）
    export CFLAGS="${CFLAGS:-} -include stdbool.h"

    ./configure \
        --prefix=/usr \
        --sysconfdir=/etc \
        --with-systemd=no \
        --without-watch8bit \
        --disable-static \
        --disable-nls

    # ★ 从 SUBDIRS 里删掉 po。
    #
    #   前提先说清楚：configure 那一步全是对的。日志为证：
    #       Copying file po/Makefile.in.in            ← autopoint 生成了
    #       checking whether NLS is requested... no   ← --disable-nls 生效了
    #
    #   断在 procps 自己的 Makefile.am：SUBDIRS 里 po 是无条件列着的，
    #   而 NLS 一关，gettext 的 po/Makefile 就没有 all 目标。
    #
    #   ★ 而 SUBDIRS 是【多行续行】的（我第一版想错了）：
    #         SUBDIRS = \
    #         <tab>local \
    #         <tab>po-man \
    #         <tab>po \          ← 在【自己那一行】
    #         <tab>testsuite
    #     行内替换只动第一行，所以删的必须是【那一整行】。
    sed -i '/^[[:space:]]*po[[:space:]]*\\$/d' Makefile

    # 检查按行来 —— 匹配真实结构，不是我以为的结构。
    if grep -qE '^[[:space:]]*po[[:space:]]*\\?$' Makefile; then
        echo "  ✗ Makefile 里还有一行就是 po（sed 没删掉）"
        grep -n -B2 -A2 -E '^[[:space:]]*po[[:space:]]*\\?$' Makefile | sed 's/^/      /'
        exit 1
    fi
    echo "  ✓ SUBDIRS 里没有 po 了"

    make
}
