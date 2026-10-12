# dash 的构建步骤 —— 只覆盖 package()
#
# [build] 表里的声明式配置负责 build()（autotools，带 out_of_tree），
# 这里只接管安装。

package() {
  # ★ 用 $SRCDIR 不是 ${BUILDDIR}。
  #
  # dash 是【在树内】构建的（spec.toml 的 [build] 没写 out_of_tree），所以
  # BUILDDIR 是空的——那是 out_of_tree 才有的变量。第一次就栽在这儿：
  #
  #     /build/steps.sh: line 7: cd: null directory
  #
  # glibc 的 steps.sh 里用的是 ${BUILDDIR}，因为它开了 out_of_tree。照抄会错。
  cd "$SRCDIR"

  make DESTDIR="$PKGDEST" install

  # ── /bin/sh ────────────────────────────────────────────────────────────
  #
  # 归 dash-binsh 子包（spec.toml 里声明的 [[subpackage]]，files 匹配
  # usr/bin/sh，provides="/bin/sh"）。
  #
  # ★ 用【相对】链接 `dash` 而不是 `/usr/bin/dash`：
  #   它和目标是同一个目录，相对链接跟着目录走，将来 rootfs 换个前缀也对。
  #   （Alpine 用的是绝对的，但那是它的选择，不是必须的。）
  #
  # ★ 为什么建在 $PKGDEST 里而不是 make install 之后另找地方：
  #   它就是这次构建的产物之一，和别的文件一样要被收集、被打包、被 strip。
  #   （strip 会跳过符号链接，不影响。）
  ln -sf dash "$PKGDEST/usr/bin/sh"
}
