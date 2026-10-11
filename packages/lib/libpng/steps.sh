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
