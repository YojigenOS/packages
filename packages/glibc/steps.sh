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

  # locale 数据：make install 不包含它们，必须单独生成。
  #
  # install-locales 会把 SUPPORTED 里全部两百多个 locale 都装上（约 200 MB）。
  # 这里改用 install-locale-files（--no-archive --no-hard-links，生成独立目录
  # 而不是打包成一个 locale-archive），并用命令行覆盖 SUPPORTED-LOCALES 只装
  # 四个。这是 localedata/Makefile 官方的选装方式，不是自己拼 localedef 命令。
  #
  # **C.UTF-8 必须在列表里。** 我原来以为不用：glibc 2.35 起 C.UTF-8 是「内置」
  # 的。那个说法只对了一半——setlocale 认它，但**它仍然需要装进 locale 数据**，
  # 否则会这样：
  #
  #     bash: warning: setlocale: LC_ALL: cannot change locale (C.UTF-8)
  #
  # 而驱动给每个构建都设了 LC_ALL=C.UTF-8（可复现构建的一部分，见 0005）。
  # 它悄悄退回 C 的话，虽然仍然确定、但那份保证就不是我们写下的那一份了。
  #
  # 没装的 locale 不影响使用——用户可以自己用 localedef 生成，
  # 那个工具就在 /usr/bin 里。
  make DESTDIR="$PKGDEST" localedata/install-locale-files \
       SUPPORTED-LOCALES="C.UTF-8/UTF-8 zh_CN.UTF-8/UTF-8 zh_TW.UTF-8/UTF-8 en_US.UTF-8/UTF-8"
}
