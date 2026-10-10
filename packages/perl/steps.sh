# perl 的构建步骤
#
# 它有自己的 Configure（大写 C），生成的是 Makefile 而不是 config.status，
# 所以套不进 autotools 那条路。

build() {
  cd "$SRCDIR"
  # 【不要】传 -Dman1dir=none。perl 的 Configure 不把 "none" 当"不装"，
  # 而是当成一个相对目录名，于是 man 页落到 $PKGDEST 的根上（/CPAN.0），
  # 既不是想要的效果，也让后面的清理打偏。
  #
  # 让它按默认装到 /usr/share/man，然后整棵删掉——干净且可预期。
  ./Configure -des \
      -Dprefix=/usr \
      -Dusethreads
  make
}

package() {
  cd "$SRCDIR"
  # 用 install.perl 而不是 install。
  #
  # perl 的 `make install` 其实是三个目标的组合：
  #
  #     install.perl   二进制 + 模块库          ← 我们要的
  #     install.man    man 页（走 installman）
  #     install.html   HTML 文档
  #
  # 后两个是 Perl 语言自己的文档，约 30 MB，而且 installman 会把 man 页
  # 写到 --destdir 的【根】上（日志里是 /build/dest/CPAN.0），因为 man1dir
  # 是空的。它们里面还有一对只差大小写的：CPAN.0 和 cpan.0。
  #
  # 直接不装它们，比装完再删干净——少一次「删漏了哪个」的机会。
  make DESTDIR="$PKGDEST" install.perl

  # 还有 POD 文档目录要删——它不在 install.man 里，而是随模块库一起装的。
  #
  # 两个理由，第二个是硬的：
  #
  # 1. 理由同上面：那是 Perl 语言自己的文档，不是系统手册页。
  #
  # 2. **它和模块目录 Pod/ 只差大小写。** 宿主是 macOS，APFS 默认大小写
  #    不敏感，这两个在 output/ 里【无法共存】——拷出来时会报一句看不懂的
  #    "File exists"，或者静默少拷。
  #
  #    Pod/（模块）留着：那是功能，perldoc 和一堆 CPAN 模块要用。
  #    pod/（文档）删掉：那是内容。
  #
  # 这不是对 perl 的特殊照顾，是「产物里不能有只差大小写的路径」这条约束
  # 的第一个实例。构建驱动现在会数一遍拷出来的项数，少了会直接点名是哪组
  # ——上两次就是它把 CPAN.0 ↔ cpan.0 找出来的。
  rm -rf "$PKGDEST"/usr/lib/perl5/*/pod
}
