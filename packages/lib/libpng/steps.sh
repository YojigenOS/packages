# libpng 的构建步骤
#
# 只覆盖 build()，为的是把并行度降下来。原因见下面。
# package() 用驱动的默认值（make DESTDIR="$PKGDEST" install）。

build() {
  ./configure --prefix=/usr --disable-static

  # ★ 串行构建。
  #
  # libpng 自己的 Makefile 有个竞态：contrib/libtests/ 下的几个测试程序
  # 需要 pnglibconf.h，而那个文件是构建时用 gawk 现场生成的
  # （scripts/options.awk + scripts/pnglibconf.dfa）。**libpng 没在
  # Makefile 里声明这个依赖**，于是并行 make 会先编 pnggetset.c、后生成
  # pnglibconf.h，编译器看到一个还没有 PNG_SETJMP_SUPPORTED 的头：
  #
  #     contrib/libtests/pnggetset.c:53: error: implicit declaration of
  #         function 'setjmp'
  #     contrib/libtests/../../png.h:977: error:
  #         'LIBPNG_WAS_COMPILED_WITH__PNG_NO_SETJMP' undeclared
  #
  # 报错完全指不到真正的原因（说的是 setjmp 没有声明，看起来像 glibc 少东西）。
  #
  # 实测：本地 -j8 侥幸过了，CI 的 -j4 撞上了。**跟并行度无关，是运气。**
  # 而 configure 的输出两边逐行一致——所以只能从"构建时发生了什么"里找。
  #
  # libpng 很小，串行也就几十秒，不值得为它把依赖补进上游的 Makefile。
  make -j1
}
