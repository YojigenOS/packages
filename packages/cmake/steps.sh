# cmake 的构建步骤 —— 用它自己的 bootstrap
#
# ./bootstrap 是一个 make 驱动的引导脚本：先编出一个最小 cmake，再用它配出
# 完整版。所以构建 cmake 不需要预装 cmake。
#
# 关掉的东西都是树里还没有的：
#   OpenSSL        —— 我们还没打包 openssl（它在 minimal 那条线上）
#   CursesDialog   —— 需要 ncurses，还没有
#   nde/cmake-gui  —— 需要 Qt，不打算要
#
# 它自带的第三方库（jsoncpp、libuv、libarchive…）默认用内置的，不依赖系统。
#
# -DBUILD_TESTING=OFF：不建测试套件。实测本地这一条就是 23 分钟里的大头——
# CMakeLibTests 是一个巨大的 C++ 构建，而构建环境不需要它。上游测试要跑的
# 话是 [build] check 的事（见 0010），不是默认构建。
#
# （注意：选项只能竖着排在续行里，不能在这些 \ 续行【中间】插注释——
#   注释会把续行截断，后面的选项会变成独立的命令。）

build() {
  cd "$SRCDIR"
  ./bootstrap \
      --prefix=/usr \
      --parallel="$(nproc)" \
      -- \
      -DCMAKE_BUILD_TYPE=Release \
      -DBUILD_TESTING=OFF \
      -DCMAKE_USE_OPENSSL=OFF \
      -DBUILD_CursesDialog=OFF \
      -DBUILD_QtDialog=OFF
  make
}

package() {
  cd "$SRCDIR"
  make DESTDIR="$PKGDEST" install
}
