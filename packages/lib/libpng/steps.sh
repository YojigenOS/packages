# libpng 的构建步骤
#
# ⚠️ 这个包有一个【尚未解决】的问题，见文件末尾。串行构建是无害的，但
#    **它不是解药**——我一开始以为是并行竞态，实测把它证伪了。

# 串行。
#
# libpng 很小，串行也就几十秒。我一度以为它在并行下会竞态：contrib/libtests
# 需要构建时生成的 pnglibconf.h，而并行 make 可能先编测试、后生成头文件。
#
# **但那个假设是错的。** 把 MAKEFLAGS 设成 -j1 之后 CI 依然失败，而且日志里
# 编译命令【完全是串行的】、`pnglibconf.h` 也【已经正确生成】——然后编译照样
# 报错。所以根本不是竞态。
#
# 留着串行是因为它无害、而且这个包小。真正的修复在别处。
export MAKEFLAGS=-j1

build() {
  ./configure --prefix=/usr --disable-static
  make
}

# ============================================================================
# 未解决：x86_64 + 我们自己的构建环境 → pnglibconf.h 里 SETJMP 被关掉
# ============================================================================
#
# 现象（CI 自举卡在 [31/42]）：
#
#     contrib/libtests/pnggetset.c:53: error: implicit declaration of function 'setjmp'
#     contrib/libtests/../../png.h:977: error:
#         'LIBPNG_WAS_COMPILED_WITH__PNG_NO_SETJMP' undeclared
#
# **报错完全指不到真正的原因**——说的是 setjmp 没声明，看起来像 glibc 少东西。
#
# 已经确定的事实：
#
#   x86_64 + Debian 构建环境     → 过 ✓
#   x86_64 + 我们的 base-devel   → 失败 ✗   ← 本地一分钟就能复现
#   aarch64 + 我们的 base-devel  → 过 ✓
#
#   两份 pnglibconf.out 的**行号完全一致**，只有第 113 行不同：
#
#     Debian:  #define PNG_SETJMP_SUPPORTED        ← 开着
#     我们:    /*#undef PNG_SETJMP_SUPPORTED*/     ← 关掉了
#
#   而两次 configure 的输出只有四处不同（同一架构、同一配方）：
#
#     -std=gnu23          vs  none needed      （gcc 14 要显式开 C23，gcc 16 默认就是）
#     /usr/bin/ld         vs  /usr/x86_64-pc-linux-gnu/bin/ld
#     gcc -std=gnu23 -E   vs  gcc -E
#     file                vs  :                ← ★ file 命令在我们的环境里【不存在】
#
# ★ 已经排除的两个假设（都实测过，都证伪了）：
#
#   ✗ C23：加 CFLAGS=-std=gnu17 重跑，SETJMP 照样是关的。
#   ✗ file 缺失：往容器里塞一个 file，SETJMP 照样是关的。
#
# ★★ 而更要紧的是：**不是只有 SETJMP 出问题**。
#
#   我们的 pnglibconf.out 里【全部选项都是 undef】：
#
#       /*#undef PNG_16BIT_SUPPORTED*/
#       /*#undef PNG_ALIGNED_MEMORY_SUPPORTED*/
#       /*#undef PNG_BENIGN_ERRORS_SUPPORTED*/
#       ... 一直到 PNG_SETJMP_SUPPORTED
#
#   整份生成的配置是空的。setjmp 只是第一个被编译器撞上的那个。
#
#   对照：在 Debian 环境里 `./configure` 之后 pnglibconf.h 【有】；
#         在我们的 base-devel 里 —— 【没有】。
#
#   所以真正的问题不是"某个探测失败"，而是 **libpng 的配置生成链路在我们
#   的 x86_64 base-devel 里整个没跑通**。aarch64 上它是通的。
#
# 下次接手的入手点：
#
#   1. 从 scripts/options.awk 的输入看起：生成用了
#      `version=search ./pngconf.h ./scripts/pnglibconf.dfa`，而"全 undef"
#      的典型成因是 search 没找到版本标记。对比两个环境里 pngconf.h 的内容
#      和 gawk 的行为差异。
#   2. 现成的对照：两个环境里 pnglibconf.out 的 diff（我们的 233 行全是
#      undef；Debian 那边 configure 已经生成了可用的头）。
#   3. 对照命令见下面。
#
#        SKIRT_BUILDENV=yojigenos/buildenv   ./tools/skirt build libpng --arch x86_64
#        SKIRT_BUILDENV=yojigenos/base-devel ./tools/skirt build libpng --arch x86_64
#
#      注意本地要先有 yojigenos/base-devel:x86_64 这个标签——发布出来的是
#      edge-x86_64，selfhost job 自己打的裸标签：
#
#        docker pull --platform linux/amd64 yojigenos/base-devel:edge
#        docker tag yojigenos/base-devel:edge yojigenos/base-devel:x86_64
#
# ============================================================================
# 2026-10-11 续查：断点定位到 options.awk 的 version=search
# ============================================================================
#
# 生成的链路（Makefile 的 pnglibconf.out 规则）：
#
#     gawk -f scripts/options.awk out=pnglibconf.tf4 version=search \
#          ./pngconf.h ./scripts/pnglibconf.dfa ./pngusr.dfa     ← ①
#     gawk -f scripts/options.awk out=pnglibconf.tf5 pnglibconf.tf4  ← ②
#     mv pnglibconf.tf5 pnglibconf.c
#     gcc -E ... -DPNG_BUILDING_SYMBOL_TABLE pnglibconf.c > tf1      ← ③
#     gawk -f scripts/dfn.awk out=tf2 tf1
#     mv tf2 pnglibconf.out                                          ← ④
#
# 实测 ①② 都产出了东西（pnglibconf.c 有 10545 行、含 SETJMP 16 处），
# ③④ 也跑完了——但 **④ 的结果里每一个选项都是 undef**。
#
# 说明 pnglibconf.c 里的 #ifdef 链【全部走到了 #else】。而那些开关来自 ①：
# `version=search ./pngconf.h` —— 意思是"从 pngconf.h 里搜出支持标记"。
# 我们的 gawk 5.4.1 在那一步【什么都没搜到】，Debian 的 gawk 5.2.1 搜到了。
#
# 注意 ①② 单独跑在两边都能看到 "option: SETJMP enabled"——所以光看中间产物
# 是看不出来的，要看 ① 生成的 pnglibconf.c 里那些 #ifdef 实际取哪一支。
#
# 下一步：把两个环境里 ① 的产物 pnglibconf.c diff 一下（或者直接看它开头
# 那些 #ifdef/#else 的走向）。两个 gawk 对 options.awk 里 search 模式的
# 行为差异就是答案。
#
# 手动复现（一分钟）：
#
#   docker run --rm -v $PWD/<libpng源>:/w -w /w yojigenos/base-devel:x86_64 \
#     bash -c './configure --prefix=/usr --disable-static >/dev/null && \
#              make pnglibconf.out && sed -n "113p" pnglibconf.out'
#
# 本地要有 yojigenos/base-devel:x86_64 这个标签（发布的是 edge-x86_64）：
#   docker pull --platform linux/amd64 yojigenos/base-devel:edge
#   docker tag yojigenos/base-devel:edge yojigenos/base-devel:x86_64
