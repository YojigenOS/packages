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
    # ★ meson 已经把库和模块装到 $PKGDEST 了（驱动先跑 meson install，
    #   再调我们的 package()）。这里只补 pam.d。
    #
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
