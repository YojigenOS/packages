# openssh 的构建步骤
#
# build() 用驱动的 autotools 默认（./configure && make，选项见 spec.toml）。
# 这里只重定义 package()，因为有几件事必须自己做对：
#
#   1. 主机密钥一个都不许带出去（每台机器自己生成）
#   2. /var/empty —— 权限分离的沙箱，711、root
#   3. 首启钩子（生成密钥 + 起 sshd）
#   4. root 的默认公钥（0013「还没定 #1」的临时答案）
#   5. sshd_config 补 Subsystem sftp —— 没有它 scp/sftp 都不能用
#
# 可用的环境变量（由驱动注入）：
#   $PKGDEST   安装目标，等于最终根文件系统里的 /
#   $PKGDIR    本配方目录的副本，静态文件在 $PKGDIR/files/ 下
#   $SRCDIR    源码树
#   $WORK      容器内工作区

package() {
    # ★ 重定义 package() 就等于把默认那一步接管过来，所以这行不能少。
    #   驱动给 autotools 的默认步骤就是这一条（build.py）。
    make DESTDIR="$PKGDEST" install

    # ── 1. 主机密钥：一个都不许带出去 ────────────────────────────────────
    #
    # 上游的 install 在【没有 DESTDIR】时会顺手生成主机密钥；有 DESTDIR 时
    # 一般不会——但"一般不会"不是"一定没有"，而这件事的后果是所有机器共用
    # 同一把主机密钥。
    #
    # 所以不推断，直接删，然后确认真的没有了：检查写在同一个函数里，
    # 而不是留给"下次谁记得"。
    rm -f "$PKGDEST"/etc/ssh/ssh_host_*
    if ls "$PKGDEST"/etc/ssh/ssh_host_* >/dev/null 2>&1; then
        echo "openssh: 打包后仍然存在主机密钥，拒绝这个产物" >&2
        return 1
    fi

    # ── 2. 权限分离的沙箱目录 ───────────────────────────────────────────
    #
    # ★ 只设权限，不设属主：package() 是以【宿主用户】降权跑的（构建阶段
    #   降权是驱动故意的），改属主会被拒：
    #       install: cannot change owner and permissions of '.../var/empty'
    #   属主由打包统一归零（tar 用 --owner=0 --group=0），不需要在这里管。
    install -d -m 711 "$PKGDEST/var/empty"

    # ── 3. 首启钩子 ─────────────────────────────────────────────────────
    #
    # /init 按名字顺序跑 /usr/lib/yojigen-os/boot.d/ 下的东西。这个钩子做两件
    # 事：没有主机密钥就生成，然后起 sshd。
    install -Dm755 "$PKGDIR/files/20-sshd" "$PKGDEST/usr/lib/yojigen-os/boot.d/20-sshd"

    # ── 4. root 的默认公钥 ──────────────────────────────────────────────
    #
    # ★ 这是 0013「还没定 #1」的临时答案。DD 通道没有任何注入点（reinstall 不会
    #   替我们改盘），所以先构建时烤一把进去。要换人，改 files/authorized_keys。
    #
    #   公钥不是秘密，打进镜像没有泄露问题；它的作用只是"谁能登进来"。
    # ★ 属主必须是 root —— 这两行原来【没写 -o/-g】，于是包里的属主是构建用户。
    #
    #   后果实测撞到：在【运行中的系统】上 `apk upgrade openssh`，装完之后
    #   SSH 直接进不去了：
    #
    #       root@…: Permission denied (publickey,…)
    #       ssh -v: 密钥 offer 了，而 server 说 "Authentications that can continue"
    #
    #   而密钥本身是对的（两边指纹一模一样）。拒它的是 sshd 的 StrictModes ——
    #   它会一路查 ${HOME}、${HOME}/.ssh、authorized_keys 的属主。
    #
    #   ★★ 为什么"镜像里那份是好的、升级来的这份是坏的"：
    #      拼 rootfs（pantsu）那一步会把属主归一成 root，
    #      而【运行中 apk 装包】保留包里的属主 —— 也就是构建用户。
    #      同一个包，两条路，结果不一样。
    #
    #   ★ 而 20-sshd 那个钩子早就知道这一类（它专门写过"只 chmod 是不够的"），
    #     但它只兜了 /root 和 /var/empty，没兜 .ssh —— 而它是【启动】钩子，
    #     运行中升级根本不会跑它。所以包本身必须是对的。
    install -d -m 700 -o root -g root "$PKGDEST/root/.ssh"
    install -Dm600 -o root -g root "$PKGDIR/files/authorized_keys" \
        "$PKGDEST/root/.ssh/authorized_keys"

    # ── 5. sshd_config 的补充 ───────────────────────────────────────────
    #
    # 上游那份全是注释掉的默认值，能用。唯一缺的是 SFTP 子系统：新版 scp
    # 走的也是 SFTP，所以没有它 `scp` 直接不可用。
    #
    # 端口和 root 登录都用默认：22、prohibit-password（即只认公钥）。
    cat >> "$PKGDEST/etc/ssh/sshd_config" <<'EOF'

# ── Yojigen OS 补充 ──────────────────────────────────────────────────────
# 没有这一行，scp 和 sftp 都不能用：新版 scp 走的就是 SFTP 子系统。
Subsystem sftp /usr/libexec/sftp-server
EOF
}
