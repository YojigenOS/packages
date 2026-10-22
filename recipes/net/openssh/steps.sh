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
    # ★ 只设权限，不设属主 —— 而理由在 2026-10-14 15:44 变过一次：
    #
    #   写这行的时候（00:24）package() 是以【宿主用户】降权跑的，`chown` 会
    #   被拒（`install: cannot change owner and permissions of '.../var/empty'`）。
    #   而 20 分钟后驱动改了：**package() 现在以 root 跑**（那次改动的第一个
    #   动机就是本文件下面那个 `/root/.ssh` 的属主）。
    #
    #   所以 `-o root` 现在【做得到】。这里仍然不设，是因为不需要：
    #   属主由打包统一归零（`skirt pack` 里 `chown -R 0:0`，tar 再走
    #   `--owner=0 --group=0`）。**而这一行留着是对的** —— 它是那套归零的
    #   一道独立保险，而不是对某种限制的让步。
    install -d -m 711 "$PKGDEST/var/empty"

    # ── 3. 首启钩子 ─────────────────────────────────────────────────────
    #
    # /init 按名字顺序跑 /usr/lib/yojigen-os/boot.d/ 下的东西。这个钩子做两件
    # 事：没有主机密钥就生成，然后起 sshd。
    # ★ 2026-10-22：从 boot.d 搬到【服务定义】（决策记录 0024）。
    #   boot.d 是系统的启动阶段，不是外部软件的服务表。而"要不要开机自启"
    #   由风味声明（`services = ["sshd"]`），不再由"装没装这个包"决定。
    install -Dm755 "$PKGDIR/files/services/sshd" \
        "$PKGDEST/usr/lib/yojigen-os/services/sshd"

    # ── 3b. "保证 root 公钥在"那个小工具 ────────────────────────────────
    #
    # ★ 一件事三个触发点，而逻辑只写这一份：
    #
    #       post-install     装完之后          ← 配方目录的 scripts/
    #       post-upgrade     升级之后          ← ★ 而它必需，见那个脚本
    #       boot.d/20-sshd   开机时            ← 调它
    #
    #   三个入口都 exec 它。写三份就会"改一处忘一处"，而这一格出的问题
    #   恰好全是那个形状。
    install -Dm755 "$PKGDIR/files/ensure-authorized-keys" \
        "$PKGDEST/usr/libexec/yojigen-os/ensure-authorized-keys"

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
    # ★★ 这一段在 2026-10-15 改写过一次 —— 它原来的判断【已经作废】。
    #
    #   它原来写的是「`-o root` 在这里【做不到】，因为构建是降权跑的（驱动里
    #   setpriv）」。**那句话写下的 20 分钟之后就不再成立了**：驱动改成
    #   package() 以 root 跑，而那次改动的第一个动机就是这个目录。
    #
    #   所以现在 `install -o root` 是【做得到】的：
    #
    #       install -o root -g root -d -m 700 "$PKGDEST/root/.ssh"
    #
    #   ——也就是说这一处可以直接写对，不必绕。
    #
    # ★ 而 20-sshd 那个钩子【仍然必要】，只是理由不同了：
    #
    #       apk 装包     →  按包里的属主落盘（现在是 root ✓）
    #       apk 升级     →  **保留已装文件的真实 uid** —— 而一台机器上
    #                        /root/.ssh 可能是别的属主
    #
    #   所以钩子管的是【升级那条路】，不是"包做不到"。它原来修了 /root 和
    #   /var/empty、漏了 .ssh，那是它自己的缺口。
    #
    #   ★ 而为什么曾经会不一样：驱动里 `chown -R "$HOSTUID:$HOSTGID" "$WORK"`
    #     把【所有包】的属主都归成构建用户。pantsu 拼 rootfs 时归一成 root，
    #     所以镜像里一直是对的；而运行中 apk 装包保留真实 uid。两条路的结果
    #     不一样 —— 那才是这个钩子存在的原因。
    # ★★★ 而【不装成 /root/.ssh/authorized_keys】—— 这是这一版真正的改动。
    #
    #   装成那个路径的后果实测撞了两次（第 3 轮一次、刚才一次），而两次都是
    #   同一件事：**那个文件归包所有，于是每次 `apk upgrade openssh` 都会用
    #   包里那份把盘上那份替换掉。**
    #
    #   而盘上那份是【运行中的机器的身份】—— 用户往里加了自己的钥匙，
    #   升级一次就没了。属主对不对（上一版修的那个）只决定了"替换之后
    #   会不会断"，而它救不了"替换本身就发生了"。
    #
    #   所以换成一份【模板】，由启动钩子在文件不存在时铺上去：
    #
    #       模板（包拥有）  /usr/share/yojigen-os/ssh/authorized_keys.default
    #       实际文件（不归包）  /root/.ssh/authorized_keys
    #
    #   ★ 而"构建时烤一把进去"这个目的照样达到 —— 钩子是在 sshd 起来
    #     【之前】跑的，所以第一次启动就有钥匙可用。
    #
    #   ★ 两条边界都要想清楚：
    #     · 升级：包不再拥有那个文件 → apk 不碰它 → 用户加过的东西留着 ✓
    #     · 全新装机：钩子铺模板 → 第一次启动就能登 ✓
    #     而"用户自己删了想重新要一份"也简单：删掉那个文件，重启 ✓
    install -Dm644 "$PKGDIR/files/authorized_keys" \
        "$PKGDEST/usr/share/yojigen-os/ssh/authorized_keys.default"

    # ★ 而【已经装过它的旧机器】上，那个文件还留着 —— 这一版不会去删它
    #   （删了会把正在用的钥匙弄没），也不会去改它。钩子会照看它的属主。
    #   换句话说：**从现在起不再动它。**

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
