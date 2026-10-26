# at —— 一次性定时任务
#
# ★★★ 一个编译期硬假设要处理（2026-10-26）：
#
#   `atd.c` 第 586-590 行：
#
#       #if defined(SENDMAIL)
#           execl(SENDMAIL, "sendmail", "-i", mailname, NULL);
#       #else
#       #error      "No mail command specified."
#       #endif
#
#   而 `configure.ac` 第 105 行：
#
#       AC_PATH_PROG(SENDMAIL, sendmail, , $PATH:/usr/lib:/usr/sbin)
#       if test "$SENDMAIL" != "" ; then
#           AC_DEFINE_UNQUOTED(SENDMAIL, ...)
#       fi
#
#   **没有 sendmail 就不定义那个宏，然后 #error 让编译失败。**
#   那是上游的一个假设："每台 Unix 都有 sendmail"。
#
# ★★ 而"发邮件"在 at 里是**附加行为** —— 任务输出寄给用户，
#   只有 `at -m` 强制发信时才必要。**核心功能（排队/执行/atq/atrm）
#   完全不需要它。**
#
# ★ 修法：把 SENDMAIL 定义成标准路径 `/usr/sbin/sendmail`。
#   运行时没有那个程序 → execl 失败 → perr 打一行错，**任务本身照跑**。
#   而标准发行版也都是这么编的（Debian 的 at 就是 SENDMAIL=/usr/sbin/sendmail，
#   它不依赖 sendmail 包存在）。

build() {
    # ★ 先跑 autogen（Debian 的 orig.tar.gz 里有 configure，但保险起见）
    [ -x ./configure ] || sh ./autogen.sh 2>/dev/null || autoreconf -fi

    ./configure \
        --prefix=/usr \
        --sysconfdir=/etc \
        --with-etcdir=/etc/at \
        --with-jobdir=/var/spool/at/jobs \
        --with-atspool=/var/spool/at/spool \
        --with-daemon_username=daemon \
        --with-daemon_groupname=daemon \
        --with-systemdsystemunitdir=/usr/lib/systemd/system

    # ★★★ 补 SENDMAIL 宏（configure 找不到 sendmail 时不会定义它）
    #
    #   ★ 用 CFLAGS 注入而不是改 configure.ac —— 改上游源码下次升级就丢。
    #     而 `-DSENDMAIL=` 这种"预先定义"在 C 里是标准做法。
    make -j"${JOBS:-4}" CFLAGS="${CFLAGS:-} -DSENDMAIL='\"/usr/sbin/sendmail\"'"
}

package() {
    cd "$SRCDIR"
    make install DESTDIR="$PKGDEST"

    # ★★★ spool 目录的属主（2026-10-26 撞到的第二个坑）
    #
    #   上游 `make install` 把 /var/spool/at/{jobs,spool} 建成 **root:bin 0770+T**，
    #   而 `atd` 以 **daemon:daemon** 运行（configure 的 --with-daemon_username）。
    #
    #   后果（实测 TX 上）：
    #
    #       Cannot change to /var/spool/at/jobs: Permission denied
    #       atd.service: Failed with result 'exit-code'
    #
    #   ★ 那是个 `drwxrwx--T root bin` —— daemon 用户既不是 owner 也不在 bin 组。
    #
    #   ★★ 标准发行版把这两个目录归 daemon:daemon（Debian 的 at 包里
    #      /var/spool/at 就是 daemon:daemon）。所以这里跟着改。
    #
    #      ★ 而 `chown` 用的是**数字 UID/GID**（2:2）—— 因为打包容器里
    #        `daemon` 这个名字可能不存在（我们的 base-passwd 里它在，
    #        但不要赌）。见 filesystem 那条 udev 的同类教训。
    for d in var/spool/at var/spool/at/jobs var/spool/at/spool; do
        [ -d "$PKGDEST/$d" ] && chown 2:2 "$PKGDEST/$d" 2>/dev/null || true
    done

    # ★★★ 修 atd.service 的 Type（2026-10-26 撞到的第三个坑）
    #
    #   上游给的 unit 是：
    #
    #       ExecStart=/usr/sbin/atd -f       ← -f 是"前台"
    #       （Type 没写 → 默认 simple）
    #
    #   而**那个组合是错的**。实测 TX 上：
    #
    #       Type=simple + -f      → systemd 报 active，而 "Control group is empty"
    #                               （atd 立刻没了，任务永远不执行）
    #       Type=forking + 不带 -f → active ✓
    #
    #   ★ 根因：`atd` 自己会 fork（`-f` 是"不要 fork，留在前台"，
    #     而 systemd 的 `Type=simple` 要求"进程不 fork、一直不退"）。
    #     两者叠在一起时 atd 的 fork 行为和 simple 的预期打架 ——
    #     父进程退了，systemd 认为"启动完成"，而真正干活的子进程
    #     在它自己那套里，systemd 看不见（Control group is empty）。
    #
    #   ★★ 标准发行版用 `Type=forking` + 不带 `-f`（Debian 的 atd.service
    #      就是这样）。我们沿用那个。
    #
    #   ★ 而这一条**只有真的把服务起起来才会暴露** —— 编译过、包装上、
    #     `at` 命令能排任务，而任务永远不执行。
    unit="$PKGDEST/usr/lib/systemd/system/atd.service"
    if [ -f "$unit" ]; then
        # 在 [Service] 段后插 Type=forking，并把 ExecStart 的 -f 去掉
        sed -i 's|^\[Service\]$|[Service]\nType=forking|' "$unit"
        sed -i 's|^ExecStart=/usr/sbin/atd -f$|ExecStart=/usr/sbin/atd|' "$unit"
        echo "  ✓ atd.service 改成 Type=forking（原 Type=simple + -f 会空转）" >&2
    fi

    # ★ 断言：at / atd / atq / atrm 四个都要有
    for b in at atd atq atrm; do
        [ -x "$PKGDEST/usr/bin/$b" ] || [ -x "$PKGDEST/usr/sbin/$b" ] || {
            echo "  ✗ at: $b 没装上" >&2
            return 1
        }
    done
    : 
}
