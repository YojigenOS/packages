# yojigen-os-init 的构建步骤
#
# 没有编译——这个包就是一个脚本。步骤只负责把它摆到内核会去找的位置。
#
# 可用的环境变量（由驱动注入，和 filesystem 那个包一样）：
#   $PKGDEST   安装目标，等于最终根文件系统里的 /
#   $PKGDIR    本配方目录的副本，files/ 下的东西在这里
#   $SRCDIR    空的源码目录（本包无源码）
#   $WORK      容器内工作区

build() {
    # 只有一个 C 文件要编：cttyhack。
    #
    # 它是为了消掉 tty0 上那句 "/bin/sh: 0: can't access tty" —— 详见
    # files/cttyhack.c 开头的说明。用 ${CC}（驱动会指向交叉编译器）。
    "${CC:-gcc}" -O2 -Wall -o "$WORK/cttyhack" "$PKGDIR/files/cttyhack.c"
}

package() {
    # 内核启动后会在根目录找 /init 并 exec 它。路径是约定，不是偏好。
    install -Dm755 "$PKGDIR/files/init" "$PKGDEST/init"

    # 首启钩子。/init 按名字顺序跑这个目录下的东西——它自己不该知道
    # 每一个钩子是谁。10- 排在 20-sshd 之前：先扩盘，再起服务。
    install -Dm755 "$PKGDIR/files/10-grow-root" \
        "$PKGDEST/usr/lib/yojigen-os/boot.d/10-grow-root"

    # 首启钩子，按名字顺序跑。05 排在 10（扩盘）前面：
    # 主机名要尽早设——日志、DHCP、sshd 都会用到它。
    # ★ 2026-10-22：挂 cgroup（容器运行时的前提）。名字 05，排在最前面。
    # ★★ 2026-10-22：SysV 的三个老接口（echo_success / echo_failure /
    #   log_error）。它们本来是 RedHat 的 /etc/init.d/functions 里的 shell 函数，
    #   而很多老 init 脚本直接调 —— 没有它们就报 "command not found"（不影响
    #   功能，但会把安装脚本的输出搅乱，而且让"失败"和"成功"看起来一样）。
    #   做成一个多调用的脚本，装成三个名字。
    install -Dm755 "$PKGDIR/files/sysv-echo" "$PKGDEST/usr/sbin/echo_success"
    ln -sf echo_success "$PKGDEST/usr/sbin/echo_failure"
    ln -sf echo_success "$PKGDEST/usr/sbin/log_error"

    # ★★ 2026-10-22：SystemV → systemd 的桥（决策记录 0024 的延伸）。
    #
    #   `/etc/services/` 是 0024 定的"启用"出处，而 **systemd 不认识它** ——
    #   所以换 PID 1 之前必须先有这座桥，否则 sshd / docker 都不起来。
    install -Dm755 "$PKGDIR/files/service-to-systemd" "$PKGDEST/usr/sbin/service-to-systemd"

    # ★ 2026-10-22：`service` 命令（决策记录 0024）。放 /usr/bin 而不是
    #   /usr/sbin —— 它在 PATH 上，而且不是"只有 root 该碰"的东西（看 list 谁都能）。
    install -Dm755 "$PKGDIR/files/service" "$PKGDEST/usr/bin/service"

    install -Dm755 "$PKGDIR/files/05-cgroup" \
        "$PKGDEST/usr/lib/yojigen-os/boot.d/05-cgroup"

    install -Dm755 "$PKGDIR/files/05-hostname" \
        "$PKGDEST/usr/lib/yojigen-os/boot.d/05-hostname"

    # cttyhack：给 tty0 上那个 shell 一个控制终端（见 build() 那段）。
    install -Dm755 "$WORK/cttyhack" "$PKGDEST/usr/libexec/cttyhack"

    # ★ reboot / poweroff —— 2026-10-15 加的，理由见那两个文件的开头。
    #
    #   一句话：**util-linux 早就不提供它们了**（2.42 的源码里一个都没有），
    #   而别人的系统靠 busybox（Alpine）或 systemd（Debian）——
    #   我们两个都没有，于是真机上 `reboot` 是 command not found。
    #
    #   放 /usr/sbin：util-linux 的 configure 里 `--sbindir=/usr/sbin`，
    #   所以那个目录本来就在 PATH 上。
    install -Dm755 "$PKGDIR/files/reboot"   "$PKGDEST/usr/sbin/reboot"
    install -Dm755 "$PKGDIR/files/poweroff" "$PKGDEST/usr/sbin/poweroff"
}
