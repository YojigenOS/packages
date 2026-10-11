# libpng 的构建步骤
#
# 只做一件事：让这个包【整体】串行构建。

# ★ 串行，而且必须在文件顶层导出。
#
# 原因一：libpng 自己的 Makefile 有竞态。
#
# contrib/libtests/ 下的几个测试程序需要 pnglibconf.h，而那个文件是【构建时】
# 用 gawk 现场生成的（scripts/options.awk + scripts/pnglibconf.dfa）。
# **libpng 没在 Makefile 里声明这个依赖**，于是并行 make 会先编 pnggetset.c、
# 后生成 pnglibconf.h，编译器看到一个还没有 PNG_SETJMP_SUPPORTED 的头：
#
#     contrib/libtests/pnggetset.c:53: error: implicit declaration of function 'setjmp'
#     contrib/libtests/../../png.h:977: error:
#         'LIBPNG_WAS_COMPILED_WITH__PNG_NO_SETJMP' undeclared
#
# 报错完全指不到真正的原因——说的是 setjmp 没有声明，看起来像 glibc 少东西。
#
# 原因二：为什么导在顶层、而不是写在 build() 里。
#
# 驱动是通过环境变量 MAKEFLAGS=-j<N> 把并行度传给所有 make 的。第一次修的
# 时候我只覆盖了 build() 里那条 make，结果**失败发生在 package() 的
# `make install` 里**——它照样带着 -j4，照样竞态，症状一字不差。
#
# **修一个竞态，要把它出现的每一处都盖上，而不只是你第一次看见它的那一处。**
#
# libpng 很小，串行也就几十秒，不值得为它去改上游的 Makefile。
export MAKEFLAGS=-j1

build() {
  ./configure --prefix=/usr --disable-static
  make
}

