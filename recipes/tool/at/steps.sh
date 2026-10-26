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

    # ★ 断言：at / atd / atq / atrm 四个都要有
    for b in at atd atq atrm; do
        [ -x "$PKGDEST/usr/bin/$b" ] || [ -x "$PKGDEST/usr/sbin/$b" ] || {
            echo "  ✗ at: $b 没装上" >&2
            return 1
        }
    done
    : 
}
