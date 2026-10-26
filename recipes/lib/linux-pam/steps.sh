# linux-pam —— 可插拔认证模块
#
# ★ meson 构建（`type = "meson"`），所以这里只做 meson 不管的那部分：
#   **/etc/pam.d/ 下的服务配置**。
#
# ★★ 那些文件是【我们自己写的】—— 上游 linux-pam 只提供 `conf/pam.conf`
#   （单个大文件），而 `/etc/pam.d/login` 那种按服务拆开的配置**由各个
#   应用的包提供**（或者由发行版的 libpam-runtime，而它只装 `other`）。
#   实测：Debian 的 libpam-runtime 里 /etc/pam.d 只有一个文件。
#
# ★★★ 每一条引用的模块都必须真实存在。实测 linux-pam 1.7.3 编出 40 个模块：
#
#       pam_access  pam_canonicalize_user  pam_debug  pam_deny  pam_echo
#       pam_env  pam_exec  pam_faildelay  pam_faillock  pam_filter  pam_ftp
#       pam_group  pam_issue  pam_keyinit  pam_limits  pam_listfile
#       pam_localuser  pam_loginuid  pam_mail  pam_mkhomedir  pam_motd
#       pam_namespace  pam_nologin  pam_permit  pam_pwhistory  pam_rhosts
#       pam_rootok  pam_securetty  pam_setquota  pam_shells  pam_stress
#       pam_succeed_if  pam_time  pam_timestamp  pam_umask  pam_unix
#       pam_usertype  pam_warn  pam_wheel  pam_xauth
#
#   而【没有】：pam_selinux / pam_console / pam_lastlog / pam_sepermit /
#   pam_tally / pam_systemd —— 那些要 SELinux / systemd 的对应依赖。
#
#   ★ 引用一个不存在的模块的后果是 **PAM 直接失败**（不是"忽略那一行"），
#     于是 login / su 完全用不了 —— 比"没有 pam.d"更糟。所以那份清单是
#     逐条核对过的，不是照抄 Debian 的。

build() {
    # meson 那部分由驱动做（type = "meson"），这里不需要 steps.sh 的 build。
    #
    # ★ 但驱动对 `type = "meson"` 会跳过 steps.sh 的 build()，只在 package()
    #   阶段调我们 —— 所以这个函数留空是故意的（写出来是为了说明这件事）。
    :
}

package() {
    # ★★★ 必须【自己】跑 meson install（2026-10-26 撞到的）。
    #
    #   我原来的理解是错的：以为"驱动对 meson 类型会先跑 meson install，
    #   再调我们的 package()，所以这里只补 pam.d 就行"。实际不是 ——
    #   `steps.sh` 里的 `package()` 是**整个覆盖**驱动生成的那个，
    #   包括里面那句 `DESTDIR="$PKGDEST" meson install -C _build`。
    #
    #   后果（实测）：payload 里 15 个文件 = 13 个 pam.d + 两个清单文件，
    #   **libpam.so 和 40 个模块一个都没有**。而构建日志一路绿灯，
    #   最后是 `test_kind` 那条断言抓住的：
    #
    #       AssertionError: linux-pam: lib 但没有 .so/.a
    #
    #   ★ 这就是"覆盖函数"的代价：覆盖的是**整个函数**，不是"在它后面追加"。
    #     所以每一份覆盖 package() 的 steps.sh 都要自己把默认那一步补回来。
    #     （`custom` 类型没有默认那一步，所以它们不受影响 —— 只有
    #      autotools/meson/cmake 这三种有默认 install 的类型要注意。）
    # ★★★ 而且要**先 cd 回源码根**。驱动版本里那句 `cd "$SRCDIR"` 在
    #   run.sh 的**第 2 行**（脚本顶部），而 `run.sh package` 是一个
    #   **独立进程** —— 它不会继承 build 那一次的 cwd。
    #
    #   所以覆盖 package() 之后，那句 cd 也没了，于是：
    #
    #       Install data not found. Run this command in build directory root.
    #
    #   ★ 这是"覆盖整个函数"这件事的第二面：不只是丢那句 meson install，
    #     连脚本顶部的 `cd "$SRCDIR"` 也不在作用域里了 —— 因为它属于
    #     run.sh 的顶层，而 `package` 分支是被单独调用的一次。
    cd "$SRCDIR"
    DESTDIR="$PKGDEST" meson install -C _build

    # ★ $PKGDIR 是【本配方目录的副本】，files/ 下的东西在 $PKGDIR/files/ ——
    #   不是 $SRCDIR（那是解开的源码）。见 yojigen-os-init 的 steps.sh 开头
    #   那行注释。
    install -d "$PKGDEST/etc/pam.d"
    for f in "$PKGDIR"/files/pam.d/*; do
        [ -f "$f" ] || continue
        install -m 644 "$f" "$PKGDEST/etc/pam.d/$(basename "$f")"
    done

    # ★ /etc/pam.d/other 必须是"拒绝一切"的兜底 —— 它是 fail-closed 的那一层。
    #   没有专门配置的服务默认**不该**能认证通过。
    #   （那个文件的内容在 files/pam.d/other 里，这里只做存在性断言。）
    [ -f "$PKGDEST/etc/pam.d/other" ] || {
        echo "  ✗ /etc/pam.d/other 没装上 —— 那是 fail-closed 的兜底，必须有" >&2
        return 1
    }
}
