# apk-tools 的构建步骤。
#
# 可用的环境变量（由驱动注入）：
#   $PKGDEST    安装目标，等于最终根文件系统里的 /
#   $PKGDIR     本配方目录的副本
#   $SRCDIR     源码目录
#   $PKGARCH    目标架构名，比如 aarch64
#
# ★ $BUILDDIR 只有配了 out_of_tree 的配方才有，这个包没有——驱动生成的
#   header 已经 cd 到 $SRCDIR 了，所以直接用相对的 _build。
#
# ★ build() 和 package() 都重定义了。
#
#   驱动对 meson 类型的默认 build() 是一句不带选项的 `meson setup _build`，
#   而我们需要九个选项。默认的 package() 只有 `meson install`，而我们要在
#   装完之后补几个运行时目录。steps.sh 一旦定义同名函数就【整个覆盖】默认，
#   所以两个都得自己写。

# 构建目录。驱动生成的 header 里已经 cd "$SRCDIR"，所以这是个相对路径。
_build=_build

build() {
  # ★ meson setup <构建目录> <选项>，不是旧版的 `meson <构建目录>`。
  #
  # 选项照 Alpine 的 APKBUILD：
  #
  #   --auto-features=disabled   没显式打开的都不打开
  #   --sbindir=/sbin            apk 的二进制装到 /sbin（不是 /usr/bin）
  #   -Darch=$PKGARCH            ★ 目标架构名
  #   -Ddocs=disabled            要 scdoc，我们没有
  #   -Dhelp=disabled            要 lua，我们不要（--help 的用法还在）
  #   -Dlua/-Dpython/-Dtests=disabled
  #   -Dzstd=disabled            不装 zstd 支持
  #
  # ★ 不设 url_backend：默认就是 libfetch——apk 自己做 HTTP，不需要 curl。
  #   0006 说过这一点，它的字符串表里也确实没有 wget 相关符号。
  #
  # ★ -Darch 为什么必须显式传：不传的话 apk 会在运行时自己探测架构，而架构
  #   决定它去仓库的哪个子目录取包（<repo>/<arch>/APKINDEX.tar.gz）。探错了
  #   它就会去找一个不存在的路径，而且报的错和"仓库里没有这个包"长得一样。
  #   显式传等于把这一整类不确定性消掉。
  meson setup "$_build" \
    --prefix=/usr \
    --sbindir=/sbin \
    --auto-features=disabled \
    --bindir=/sbin \
    -Darch="$PKGARCH" \
    -Ddocs=disabled \
    -Dhelp=disabled \
    -Dlua=disabled \
    -Dpython=disabled \
    -Dtests=disabled \
    -Dzstd=disabled

  meson compile -C "$_build"
}

package() {
  d="$PKGDEST"

  DESTDIR="$d" meson install --no-rebuild -C "$_build"

  # ★ 运行时要用到的状态目录。Alpine 在它的 package() 里显式建这几个：
  #
  #     lib/apk/db                 已装包的数据库
  #     lib/apk/exec               脚本的执行目录（apk 在那儿跑 .pre-install）
  #     etc/apk/keys               信任的公钥——我们自己的签名公钥放这儿
  #     etc/apk/protected_paths.d  受保护路径的规则
  #     var/cache/apk              包缓存
  #
  # 不建它们 apk 第一次跑就会报错。
  #
  # ★ /etc/apk/repositories 【不在】这个列表里。那是「装哪些仓库」——是
  #   发行版的决定，不是包管理器该带的。distro 的 assemble.sh 会写它。
  install -d \
    "$d/lib/apk/db" \
    "$d/lib/apk/exec" \
    "$d/etc/apk/keys" \
    "$d/etc/apk/protected_paths.d" \
    "$d/var/cache/apk"

  # 预置空的数据库文件。Alpine 的产物里这三个都是 0 字节，照做。
  : > "$d/lib/apk/db/installed"
  : > "$d/lib/apk/db/scripts"
  : > "$d/lib/apk/db/triggers"
}
