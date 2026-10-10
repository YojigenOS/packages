# ccache 的构建步骤 —— 除了默认安装，还要补代理符号链接
#
# 上游的 cmake 只做一件事：
#
#     install(TARGETS ccache DESTINATION ${CMAKE_INSTALL_BINDIR})
#
# 也就是说它只装那个二进制，**不建代理符号链接**。而那正是 ccache 的用法：
# /usr/lib/ccache/gcc 是指向 ccache 的链接，ccache 看自己是被谁调用的，
# 就知道该代理哪个编译器。没有这些链接，装了等于没装——构建照旧直接调 gcc，
# 缓存一个字节都不进。
#
# 这事在 Debian 那边是打包脚本干的，所以在 Debian 的镜像里它是现成的。
# 我们自己打包，就得自己补。
#
# 符号链接用【相对路径】：payload 会被拼进 rootfs，相对链接在拼装前后
# 指向都一样，不依赖它最终落在哪个根下。
#
# 只建我们真有的编译器：gcc / g++（我们的 gcc 带 c,c++ 两种语言）、
# cc / c++（POSIX 惯例名，configure 脚本常找它们）。
# 不建 clang 之类——那会让 ccache 去找一个不存在的编译器。

package() {
  cd "$SRCDIR"
  DESTDIR="$PKGDEST" cmake --install _build

  mkdir -p "$PKGDEST/usr/lib/ccache"
  for c in gcc g++ cc c++; do
    ln -sf ../../../usr/bin/ccache "$PKGDEST/usr/lib/ccache/$c"
  done
}
