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
  # ══ SysV 的兼容骨架（2026-10-22）══
  #
  # 下面那几个 etc/init.d 和 etc/rc2.d..rc5.d 不是"为了某一个安装脚本"—— 而是
  # 【很多既有的安装脚本和守护进程都按这套约定找地方】：
  #
  #     /etc/init.d/<名字>        服务脚本
  #     /etc/rc2.d/S95<名字>      启用的软链（S = start）
  #
  # 没有这些目录，`cp ... /etc/init.d/x` 直接报 No such file or directory ——
  # 那正是 1Panel 的安装脚本在我们系统上撞到的第一堵墙。
  #
  # ★ 有了目录之后 /init 也会扫 /etc/rc2.d/S* 把它们起起来。两套并存
  #   （我们的 /etc/services/ 和 SysV 的 rc 软链），因为生态里两种写法都有。
  for p in \
    usr/bin usr/sbin usr/lib usr/libexec usr/include \
    usr/share usr/share/man usr/share/man/man1 \
    usr/local usr/local/bin usr/local/lib usr/local/sbin \
    etc etc/profile.d etc/skel etc/ld.so.conf.d \
    etc/init.d etc/rc.d etc/rc2.d etc/rc3.d etc/rc4.d etc/rc5.d \
    var/lock var/lock/subsys \
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

  # ★★ 再放一份【权威的】/etc/group 到 /usr/share/yojigen-os/（2026-10-23）。
  #
  #   理由：`scripts/post-install` 要合并 /etc/group，而它读不到 `.apk-new`
  #   的时候需要一个兜底来源 —— apk 判断"文件没被改过"时会直接覆盖、不留
  #   .apk-new。这份就是那个兜底。
  #
  #   ★ 为什么不用 /etc/group 自己：那个是"期望状态"，而合并脚本需要的是
  #     **包里的原始表**；两者在升级路径上可能不同（apk 还没覆盖）。
  install -Dm644 "$PKGDIR/files/etc/group" "$d/usr/share/yojigen-os/group.master"

  # ── utmp 记账用的三个空文件（2026-10-23 加）──────────────────────────────
  #
  # ★★ 为什么由【我们】造，而不是等开机时 tmpfiles 建：
  #
  #   实测（TX 上 BOOT -2 的 journal，那次 systemd 起不来）：
  #
  #       [3.152] Running create action for entry f /var/log/wtmp
  #       [3.152] Relabelling "/var/log/wtmp"        ← 没有 "has been created"
  #       [3.205] systemd-update-utmp: Not writing wtmp: No such file or directory
  #
  #   而 `systemd-tmpfiles-setup.service` 里写着：
  #
  #       SuccessExitStatus=DATAERR CANTCREAT
  #
  #   —— **`CANTCREAT` 被当成成功**。于是「建不出来」被吞掉了，紧接着
  #   `systemd-update-utmp` 在 50 毫秒后启动、发现文件不在、失败；而它的
  #   `Before=sysinit.target` 让整个启动停在 sysinit 上，systemd 直接退出。
  #
  #   建不出来的原因是 **`utmp` 组那时还不存在** —— 它由 `systemd-sysusers`
  #   建，而 sysusers 和 tmpfiles 之间没有硬顺序（tmpfiles 只写了
  #   `After=systemd-sysusers.service`，而 sysusers 自己也可能迟一步）。
  #
  # ★ 所以这里把「形状」直接做进镜像：**三个空文件**。
  #
  #   ★★ 而 `utmp` 组【不】写进 files/etc/group —— 试过，是错的：
  #      systemd-sysusers 会按 `basic.conf` 里 `g utmp - - -` 动态分配一个 GID
  #      （实测 TX 上分到 998），和我们硬写的 43 冲突，而 /etc/group 归
  #      filesystem 包所有 —— sysusers 改完之后 apk 会认为它"被用户改过"，
  #      下次升级 filesystem 就变成一次合并冲突。
  #
  #   ★ 属主由开机的 tmpfiles 修正：`var.conf` 里 `f /var/log/wtmp 0664 root utmp -`，
  #     而 `f` 在文件已存在时**仍会改属主/权限**（实测：root:root → root:utmp）。
  #     它的顺序由 systemd 保证（`After=systemd-sysusers.service`）。
  #
  #   ★★ 所以镜像里的状态是「文件在、属主 root:root」—— 而那正是我们要的：
  #      **"文件不存在"这个致命条件消失了**。即使 tmpfiles 那一步还有问题，
  #      `systemd-update-utmp` 也不会再因为"没有 wtmp"而失败。
  #
  # ★ 这三个文件的用途是真实的，不是为了让 systemd 高兴：
  #     /var/log/wtmp     who / last 读它
  #     /var/log/btmp     lastb 读它（失败登录）
  #     /var/log/lastlog  lastlog 读它（稀疏文件，空文件是正常初始态）
  #   三个命令我们都有（实测 who/last/lastb 在）。
  #
  # ★ `/var/run/utmp` 这里不建：/var/run 是 → /run 的软链，而 /run 是空的
  #   tmpfs，开机才挂上去。好在它在 tmpfs 上建得出来（BOOT -2 里 /run/utmp
  #   真的成功了：`"/run/utmp" has been created.`），而且 `who` 没有 utmp 时
  #   只是"没有登录记录"，不会拦住谁。
  : > "$d/var/log/wtmp"
  : > "$d/var/log/btmp"
  : > "$d/var/log/lastlog"
  chmod 0664 "$d/var/log/wtmp" "$d/var/log/lastlog"
  chmod 0660 "$d/var/log/btmp"

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
