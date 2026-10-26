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

    # ★★★ 用 root，不是 daemon（2026-10-26 第四个坑的真正修法）
    #
    #   `atd.c` 启动末尾有一段：
    #
    #       /* We don't need root privileges all the time; running under
    #        * uid and gid daemon is fine. */
    #       RELINQUISH_PRIVS_ROOT(daemon_uid, daemon_gid)
    #
    #   **atd 启动后主动降权到 daemon。** 而 `at.c` 建的任务文件是
    #
    #       -rwx------ root root        （at.c:540 的 fchmod 0700）
    #
    #   ★ 那个 fchown 回提交者的调用**被注释掉了**（at.c:337-340）：
    #
    #       /*
    #       if (fchown(fd2, real_uid, real_gid) != 0) …
    #       */
    #
    #   于是 daemon 身份的 atd **读不到 root 的任务文件** ——
    #   而且它是**静默跳过**（不是报错），所以日志里什么都没有。
    #
    #   ★★ 把 daemon_username 设成 root，`RELINQUISH_PRIVS_ROOT(root, root)`
    #      就等于"不降权" —— atd 保持 root，能读所有任务，
    #      而执行时它本来就会 setuid 到提交者（atd.c:578-581）。
    #
    #   ★★★ 注意：**注释不能夹在续行的 configure 参数中间** ——
    #      shell 的 `\` 续行遇到注释行就断了。这一版第一稿就是那样，
    #      报的是 `/build/steps.sh: line 66: --with-daemon_username=root:
    #      command not found`（参数变成了独立命令）。
    ./configure \
        --prefix=/usr \
        --sysconfdir=/etc \
        --with-etcdir=/etc/at \
        --with-jobdir=/var/spool/at/jobs \
        --with-atspool=/var/spool/at/spool \
        --with-daemon_username=root \
        --with-daemon_groupname=root \
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
        # ★★★ 而且要用 `-d`，**不是 `-f`**（2026-10-26 第六个坑）
        #
        #   `-f` 这个名字会让人以为是 "foreground"，而源码里它只是：
        #
        #       case 'f':
        #           daemon_foreground++;      ← 这个变量【从不被读取】
        #           break;
        #
        #   而真正控制 "要不要 daemonize" 的是 `daemon_debug`：
        #
        #       daemon.c:128    if (!daemon_debug) {
        #                           … dup2(devnull, 0/1/2) …
        #                           … fork 到后台 …
        #
        #   所以 `atd -f` **仍然会 daemonize** —— 它 fork 到后台，
        #   父进程退出，systemd 看到 "control group is empty"，而
        #   真正干活的子进程在 systemd 视野外。
        #
        #   ★ 而 `-d`（debug）除了不 daemonize，只多做一件事：
        #     错误信息写 stderr 而不是 syslog —— 那正好被 journal 收走，
        #     **比 syslog 更符合 systemd 的习惯**。
        #
        #   ★★ 实测对比（同一个任务、同一个目录）：
        #         atd -f  → 任务永远不执行
        #         atd -d  → AT_WORKED_OK
        sed -i 's|^ExecStart=/usr/sbin/atd -f$|ExecStart=/usr/sbin/atd -d|' "$unit"
        echo "  ✓ atd.service 改成 Type=forking（原 Type=simple + -f 会空转）" >&2

        # ★★★ 再加一条：atd 必须**以 root 运行**（2026-10-26 第四个坑）
        #
        #   实测：任务文件是 `-rwx------ root root`（`at.c:540` 设的
        #   `S_IRUSR|S_IWUSR|S_IXUSR`，而 `fchown` 把属主设成提交者）。
        #   而 `atd` 以 `daemon` 跑时**读不到它** —— 于是队列里的任务
        #   永远不执行，日志里什么都没有（它跳过读不了的文件）。
        #
        #   ★ 而 `atd.c` 里那段 `getpwnam(DAEMON_USERNAME)` 是
        #     **"执行任务时切到谁"**，不是"atd 以谁跑"：
        #
        #         atd.c:857  getpwnam(DAEMON_USERNAME)  ← 拿 uid
        #         atd.c:578  setgid(gid)  /  atd.c:581  setuid(uid)
        #                    ↑ 在执行子进程里切的
        #
        #   ★★ 所以 atd 本体必须是 root —— 它要读所有用户的任务文件，
        #      然后在执行时降权到提交者。标准发行版都是这样
        #      （Debian 的 atd.service 没有 User=，默认 root）。
        sed -i 's|^Type=forking$|Type=forking\nUser=root|' "$unit"
        echo "  ✓ atd 以 root 运行（它要读所有用户的任务文件，执行时才降权）" >&2
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
