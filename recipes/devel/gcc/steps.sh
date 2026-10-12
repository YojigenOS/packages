# gcc 的构建步骤
#
# 为什么不用声明式：
#
#   GCC 16 引入了 -latomic_asneeded 机制（为了消除 libatomic 多余的
#   DT_NEEDED 条目，PR libstdc++/123396）。但在这个版本里它的落点有
#   一处上游不一致：libatomic 把 asneeded 链接脚本写进 <builddir>/gcc/，
#   而构建期驱动搜索的是 <builddir>/host-<triplet>/gcc/，于是 libgomp、
#   libssp、libquadmath 在 configure 阶段链接测试程序时报：
#
#       /usr/bin/ld: cannot find -latomic_asneeded
#
#   OpenEmbedded、Gentoo、conda-forge 目前都在打补丁绕这个问题
#   （见 GCC PR123396 / PR123650）。
#
# 这里用的绕法比打补丁轻：先只构建编译器（all-gcc），把候选目录造出来，
# 然后把那个只有三行、内容完全固定的链接脚本预置进去，再继续构建。
# 它是个纯文本的 ld script，不依赖任何构建产物，所以可以提前放。

# 关于库路径布局（见 docs/决策记录/0001）：
#
#   YojigenOS 的库统一放 /usr/lib。但 gcc 在 aarch64-linux 上会把目标库
#   （libstdc++、libgcc_s、libatomic ...）装进 /usr/lib64 —— 这个路径由
#   gcc 的目标配置写死：
#
#       gcc/config/aarch64/t-aarch64-linux:
#       MULTILIB_OSDIRNAMES = mabi.lp64=../lib64...
#
#   顶层的 --libdir 管不到它：libstdc++ 走的是 toolexeclibdir，而它由
#   multilib 的 osdirname 推导。--libdir 仍然传，是为了让顶层那部分也
#   统一到 /usr/lib。
#
#   纠偏放在 package() 里：安装前把 $PKGDEST/usr/lib64 做成指向 lib 的
#   符号链接，安装路径不变、文件实际落进 /usr/lib。这样不必长期维护一个
#   针对 gcc 的补丁。
#
#   将来有了 base-files 之类的包，这个链接应该挪过去。
#
# 必须离开源码树构建。在源码目录里直接 configure 会把产物写进源码树，
# 路径关系更乱。gcc 官方一直明确要求在独立目录中构建。
BUILDDIR="$SRCDIR/_build"

build() {
  mkdir -p "$BUILDDIR"
  cd "$BUILDDIR"

  # 加固的默认值。各发行版都这么配，而从源码构建时默认是关的：
  #
  #   --enable-default-pie  地址随机化。本目标上本来就是默认（实测编出来的
  #                         是 DYN），显式写出来是为了换目标时不会悄悄丢掉。
  #   --enable-default-ssp  栈保护。这个默认确实是关的——实测我们编出来的
  #                         目标文件里没有 __stack_chk_fail 引用。
  #
  # 这两个是兜底：构建环境里的 CFLAGS 管大多数包，但有些包的构建系统会
  # 忽略环境 CFLAGS，编译器自己的默认值能兜住它们。
  # ★ --host 和 --target 都要传，这是【最关键】的一个包。
  #
  #   gcc 的三元组不只出现在 --version 里，它还在【路径】里：
  #
  #       /usr/lib/gcc/<三元组>/
  #       /usr/libexec/gcc/<三元组>/
  #       /usr/bin/<三元组>-gcc
  #       /usr/include/c++/<版本>/<三元组>/
  #
  #   不传的话 config.guess 给出 aarch64-unknown-linux-gnu，那些路径就带着
  #   "unknown"。实测它出现在 11 个路径和 37 个文件的内容里。
  #
  #   ★ 值从 $PKGTRIPLE 来（驱动注入），不在这里拼——规则的定义在
  #     skirtlib/common.py 的 target_triple，只有那一处。
  #
  #   ★ 为什么 --host 和 --target 都传：gcc 是交叉编译器，它会区分
  #     「跑在哪」（host）和「给谁生成代码」（target）。本机构建时两者相同，
  #     但省略任一个都会让那一段退回 config.guess。
  #
  #   autotools 类型的包不用管这个——驱动层会无条件加 --host（37 个包共用）。
  "$SRCDIR/configure" \
    --prefix=/usr \
    --libdir=/usr/lib \
    --build="$PKGTRIPLE" \
    --host="$PKGTRIPLE" \
    --target="$PKGTRIPLE" \
    --enable-languages=c,c++ \
    --enable-shared \
    --enable-threads=posix \
    --disable-multilib \
    --disable-bootstrap \
    --disable-nls \
    --disable-werror \
    --without-isl \
    --enable-default-pie \
    --enable-default-ssp

  # 第一步：只构建编译器，让 host-<triplet>/gcc/ 这个目录出现
  make all-gcc

  # 第二步：把 asneeded 链接脚本预置到驱动会搜索的目录
  for d in gcc host-*/gcc; do
    [ -d "$d" ] || continue
    cat > "$d/libatomic_asneeded.so" <<'EOS'
/* GNU ld script
   Add DT_NEEDED entry for -latomic only if needed.  */
INPUT ( AS_NEEDED ( -latomic ) )
EOS
    echo "[gcc] 预置 libatomic_asneeded.so -> $d/"
  done

  # 第三步：继续构建目标库（libgcc / libstdc++ / libgomp ...）与其余部分
  make
}

package() {
  cd "$BUILDDIR"

  # 让 gcc 的 /usr/lib64 落点并入 /usr/lib（原因见文件顶部说明）
  mkdir -p "$PKGDEST/usr"
  if [ ! -e "$PKGDEST/usr/lib64" ]; then
    ln -s lib "$PKGDEST/usr/lib64"
  fi

  make DESTDIR="$PKGDEST" install

  # ★ 补一个 cc。
  #
  # POSIX 规定 C 编译器叫 cc，很多项目的 Makefile 直接用它的默认值
  # （xxhash 就是）。**上游 gcc 自己不建这个链接**——Debian 用
  # update-alternatives 补，别的发行版也各自补。
  #
  # 这个只有在【自举】时才暴露：用 Debian 构建环境时 cc 是 Debian 的 gcc
  # 提供的，一切正常；换成我们自己的 base-devel 之后立刻死：
  #
  #     ccache: error: Could not find compiler "cc" in PATH
  #
  # 注意 gcc 自己【会】建 c++，所以只缺 cc——这也是它一直没被发现的原因。
  ln -s gcc "$PKGDEST/usr/bin/cc"
}
