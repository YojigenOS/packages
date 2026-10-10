# glibc 的构建步骤 —— 只覆盖 package()
#
# [build] 表里的声明式配置负责 build()（含 out_of_tree），
# 这里只接管安装前的准备。
#
# 为什么需要插手：不摆好 usr-merge 链接的话，glibc 会按上游 aarch64 的
# 默认布局，把东西装到三个不同的库目录：
#
#     /lib/ld-linux-aarch64.so.1     动态链接器
#     /lib64/*.so.6                  21 个运行时库
#     /usr/lib/                      开发文件（crt1.o、libc.a、.so 链接…）
#
# 这与 docs/决策记录/0001 定下的「统一 /usr/lib」冲突。
#
# 摆好链接后，上面三者合并到 /usr/lib；而 /lib/ld-linux-aarch64.so.1
# 这个被写进每个 ELF 的 interpreter 路径，仍然能通过链接解析到。
#
# 技术债：这些链接本该属于一个 base-files / filesystem 包。等做 rootfs
# 拼装时会需要那个包（目录骨架、/etc 基础文件都得有人负责），届时挪过去。

package() {
  cd "$BUILDDIR"

  mkdir -p "$PKGDEST/usr/bin" "$PKGDEST/usr/lib" "$PKGDEST/usr/sbin"
  ln -s usr/bin  "$PKGDEST/bin"
  ln -s usr/sbin "$PKGDEST/sbin"
  ln -s usr/lib  "$PKGDEST/lib"
  ln -s usr/lib  "$PKGDEST/lib64"

  make DESTDIR="$PKGDEST" install
}
