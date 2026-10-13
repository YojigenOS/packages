# tzdata 的构建步骤 —— 把 IANA 的时区数据编译成 TZif 文件。
#
# 可用的环境变量（由驱动注入）：
#   $PKGDEST    安装目标，等于最终根文件系统里的 /
#   $PKGDIR     本配方目录的副本
#   $SRCDIR     源码目录，十个区域数据文件在这儿
#   $WORK       容器内工作区

# ── 为什么不用它自己的 Makefile ─────────────────────────────────────────────
#
# tzdata 的 tarball 里【只有数据】。zic 和 zdump 的源码在另一个包（tzcode）
# 里，而它的 Makefile 假设两个包是解在一起用的——`install` 目标里写着：
#
#     cp zdump '$(DESTDIR)$(ZDUMPDIR)/.'
#     cp zic   '$(DESTDIR)$(ZICDIR)/.'
#     cp libtz.a '$(DESTDIR)$(LIBDIR)/.'
#     $(RANLIB) '$(DESTDIR)$(LIBDIR)/libtz.a'
#
# 那四行会直接失败（源文件根本不在）。
#
# `posix_only` 那个目标虽然只编数据，但它的依赖里有 `zic` 这个【目标】：
#
#     INSTALL_DATA_DEPS = zic leapseconds tzdata.zi
#     posix_only: $(INSTALL_DATA_DEPS)
#             $(ZIC_INSTALL) tzdata.zi
#
# 也就是 make 会先去编 tzcode 的 zic.o——同样没有。传 `zic=/usr/sbin/zic`
# 也绕不过去，因为那是【目标名】不是变量。
#
# 所以这里直接调 zic。那正是 posix_only 最终执行的那一条命令，只是把输入从
# 生成出来的 tzdata.zi 换成了原始的十个区域文件。

build() {
  : # 没有东西需要编译——要编的是数据，而那一步在 package() 里
}

package() {
  zoneinfo="$PKGDEST/usr/share/zoneinfo"
  install -d "$zoneinfo"

  # zic 来自 glibc。构建依赖里写了它，所以它一定在。
  #
  # ★ -b fat：写出 32 位和 64 位两个数据块。不加的话默认是 slim（只有 64 位
  #   块），文件小三分之一——实测 Asia/Shanghai 是 393 字节对 561 字节。
  #   代价只有四百来 KB，换来的是老一点的读取者也能认。Alpine 用的就是 fat
  #   （实测它的 Asia/Shanghai 正好 561 字节），我们跟它。
  #
  #   ★ 十个文件一次全传给 zic：它按 zone 名自己分目录写出去，所以
  #     /usr/share/zoneinfo/Asia/Shanghai 这种层级是它建的，不是我们摆的。
  /usr/sbin/zic -b fat -d "$zoneinfo" \
      africa antarctica asia australasia europe \
      northamerica southamerica etcetera backward factory

  # 国家码与时区名的对照表（iso3166.tab 是国家，zone.tab / zone1970.tab 是
  # 国家到时区的映射）。程序一般不读它们，是给人查的——但发行版都带，
  # Alpine 也带。三个都装，多几 KB。
  install -m 644 iso3166.tab zone.tab zone1970.tab "$zoneinfo/"

  # ★ 这个包【不管】/etc/localtime。
  #
  #   上游的 install 目标会用 zic 的 -t 参数在 /etc 下写一份 localtime，但
  #   那是"装系统"的动作，不是"装包"的动作——包管理器不该替用户决定时区。
  #   对照 Alpine 的 tzdata：它的文件列表里没有 /etc/localtime。
  #
  #   要设时区是 `ln -sf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime`，
  #   或者将来 yojigen-os-firstboot 去问。两者都不该在这个包里。
}
