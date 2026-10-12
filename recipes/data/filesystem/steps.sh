# filesystem 的构建步骤
#
# 没有源码要编译，也没有东西要下载。这里做的是搭出「系统的形状」：
# 目录骨架、usr-merge 链接、/etc 基础文件、/bin/sh。
#
# 可用的环境变量（由驱动注入）：
#   $PKGDEST   安装目标，等于最终根文件系统里的 /
#   $PKGDIR    本配方目录的副本，/etc 那些文件在 $PKGDIR/files/ 下
#   $SRCDIR    空的源码目录（本包无源码）
#   $WORK      容器内工作区

build() {
  : # 没有东西需要编译
}

package() {
  d="$PKGDEST"

  # ---------------------------------------------------------------- 目录骨架
  for p in \
    usr/bin usr/sbin usr/lib usr/libexec usr/include \
    usr/share usr/share/man usr/share/man/man1 \
    usr/local usr/local/bin usr/local/lib usr/local/sbin \
    etc etc/profile.d etc/skel etc/ld.so.conf.d \
    var var/log var/tmp var/cache var/lib \
    tmp root home run mnt opt srv proc sys dev
  do
    mkdir -p "$d/$p"
  done

  # 权限：这两个是「谁都能写，但只有属主能删自己的东西」
  chmod 1777 "$d/tmp" "$d/var/tmp"
  chmod 0700 "$d/root"

  # /var/run 指向 /run —— 现代约定，避免重启后状态残留
  ln -s ../run "$d/var/run"

  # --------------------------------------------------------------- usr-merge
  # 让 /bin /sbin /lib /lib64 都通向 usr/ 下的真实目录。
  #
  # 这不只是为了整洁：每个 ELF 里写死的 interpreter 路径是
  # /lib/ld-linux-aarch64.so.1，合并之后它靠这个链接照样解析得到。
  # 而 /lib64 → usr/lib 是让 gcc 目标库和 glibc 运行时库落进统一
  # 库目录的关键（见 docs/决策记录/0001）。
  ln -s usr/bin  "$d/bin"
  ln -s usr/sbin "$d/sbin"
  ln -s usr/lib  "$d/lib"
  ln -s usr/lib  "$d/lib64"

  # /usr/lib64 → lib 与上面那个 /lib64 是两回事，别混淆：
  #     /lib64       → usr/lib    运行时查找路径（ld.so 会找 /lib64）
  #     /usr/lib64   → lib        gcc 的安装目标（toolexeclibdir）
  # gcc 把目标库装到 /usr/lib64，靠这个链接才实际落进 /usr/lib。
  # 见 docs/决策记录/0001。
  ln -s lib "$d/usr/lib64"

  # ── /bin/sh：不在这里建 ────────────────────────────────────────────────
  #
  # 这段原来写的是：
  #
  #     ln -s bash "$d/usr/bin/sh"
  #
  # 加上一句注释说「将来若要更严格的 POSIX shell，可以另建 dash 包并改指向」。
  # 现在照做了，但做法【不是"改指向"】，而是把它整个搬出去。
  #
  # ★ 为什么搬出去：/bin/sh 指向谁是【一个策略选择】，不该写死在 filesystem
  #   里。写死意味着这个系统的 sh 永远是 bash，换不了；而且它会让人写出
  #   偷偷用了 bash 扩展、却在别的系统上炸的 #!/bin/sh 脚本——陷阱是我们
  #   这一侧造的。
  #
  # ★ 现在由 dash-binsh 提供（provides="/bin/sh"，provider_priority=100）。
  #   装哪个 binsh 包由【风味】决定：
  #
  #     dash-binsh   →  /bin/sh 是 POSIX shell，脚本真的可移植
  #     bash-binsh   →  /bin/sh 是 bash（将来要的话照 dash 那个写一个）
  #
  # ★ apk 的 provides 机制负责解析：谁声明了 provides="/bin/sh"，谁就能满足
  #   对 /bin/sh 的依赖。多个提供者时按 provider_priority 选（数字大的优先），
  #   也可以明确指定装哪一个。
  #
  # 所以这里【什么都不做】——filesystem 只搭目录骨架和 /etc 基础文件。

  # ------------------------------------------------------------- /etc 基础文件
  cp -a "$PKGDIR/files/etc/." "$d/etc/"

  # 不能依赖源文件的权限位——cp -a 会把它原样带过来，而仓库里的文件权限
  # 取决于谁在什么系统上创建的。这里显式规范化：
  #   /etc 下都是公开可读的配置，0600 会让普通用户连 os-release 都读不到
  #   只有 shadow 例外
  find "$d/etc" -type f -exec chmod 0644 {} +
  chmod 0600 "$d/etc/shadow"
}
