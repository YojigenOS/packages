# 0025 systemd 当 PID 1

**2026-10-24 定。** 它回答的是"这台机器的 1 号进程是谁"。

## 起因

0022 已经写了"上游有官方二进制就只分发，否则自己建"，而 systemd 是我们
**自己建**的那一类（`recipes/init/systemd`）。但建出来和用起来是两件事 ——
一直没切，因为 `/init` 那套（`boot.d` + `/etc/services/`）够用。

YG 说"我们为啥不用真正的 systemd 呢"，然后拍了"B 和 A 都要做，不怕 TX 重启"。

## 决定

**`server` / `minimal` 这类"上真机"的风味，PID 1 是 systemd。**
容器风味仍然用 `/init`（容器的启动是 entrypoint，不是 init — 0013）。

## 而切换之后，两套东西必须能共存

这是这一篇真正要记的：**`/init` 那套启动钩子不能只是"留在旧路径上"，
它得在 systemd 下照样跑。**

    /init      → 自己 for 循环跑 boot.d
    systemd    → yojigen-bootd.service 调 /usr/lib/yojigen-os/bootd
                 而 bootd 里跑的**还是同一份 boot.d**

★ 为什么"同一份"是硬要求：钩子**按包分发**（`10-grow-root` 在
yojigen-os-init、`15-network` 在 dhcpcd、host key 在 openssh）。要是给
systemd 复制一份清单，下次加一个钩子就得改两处 —— 那种事一定会漏。

## 这次踩的坑（每一个都花了好几轮）

### ① `set default="标题"` 在 GRUB 2.14 上不生效

改了之后重启，走的还是旧条目。而它**不报错**——静默地用了默认值。
判据：**grub 的 `set default` 只信编号。** 要用标题选，得配合
`GRUB_DEFAULT=saved` + `grub-set-default`，而那是另一套机制。

所以布局改成「第 0 项 = systemd，第 1 项 = 回退」，`set default=0`。

### ② 回退项必须真的存在

第一次切换我把主条目改成了 systemd，而回退项在 `custom.cfg` 里、
`custom.cfg` 的条目**排在主条目之前** —— 于是 `default=0` 指的是主条目，
**自动回退这条路其实不存在**，加上超时只有 1 秒，人根本来不及选。

★ 现在：超时 5 秒、回退项排第 2、默认第 0 项是 systemd。

### ③ `reboot` 早就不能用了，而我没看 `uptime -s`

`replaces` 让 systemd 的 `/usr/sbin/reboot`（→ `systemctl`）覆盖了我们的。
而 systemd 不是 PID 1 时它**什么也不做**：

    System has not been booted with systemd as init system (PID 1). Can't operate.

于是连续三次"重启"一次都没重启，`/proc/1/cmdline` 一直是 `/init`，
而我以为"改了 grub 就该生效"。

★ 判据：**改 grub 之后先看 `uptime -s`，不要看"我发了 reboot"。**

### ④ ★★★ 真正的失联原因：systemd 下没人配网卡

三次切过去都 SSH 失联。我查了 wtmp、udevd、sysinit 依赖环、journal 容量
—— 全是岔路。而真相是：

    boot.d/15-network（属于 dhcpcd 包）是【唯一】配网卡的东西
    而它【只有 /init 会跑】
    → systemd 起来了、sshd 也起来了，而机器没有 IP

**证据我早就看见了**：`systemd-timesyncd` 在 32 分钟里重试 DNS **163 次全失败**。
我把它当成"可疑项"，而不是把它读成"没有网络"。

★ 这正是 0025 里 `yojigen-bootd.service` 存在的原因。

### ⑤ udev 不能 mask —— 它不只是加载模块

原配方里写着：

> `-Dudev=false` 内核 CONFIG_MODULES 没开，没有模块加载，udev 存在的理由就没了

而 **udev 还负责把设备注册给 systemd**。mask 掉之后：

    dev-ttyS0.device      inactive (dead)     ← 没有 udev 来"插上"它
    getty.target          start waiting
    multi-user.target     start waiting
    → is-system-running 永远是 starting

实测（TX，`CONFIG_MODULES=n`）：手工起 udevd + `udevadm trigger`，
`dev-ttyS0.device` 立刻变 `active (plugged)`，`is-system-running` 变 `running`。

★ 判据：**"这件东西在我们这儿没用"要分清是"它的哪一部分"没用。**
udev 的"加载模块"没用，而"枚举设备并告诉 systemd"正是指望它干的活。

### ⑥ `systemd.log_target=console` 让日志看不见

排查时我给 cmdline 加了 `systemd.log_target=console`，想把 debug 打到串口。
结果 `systemd[1]` 在盘上的 journal 里**只有 18 条**（挂载之前那些），
之后的全部去了 ttyS0 —— 而 ttyS0 只能在 VNC 里看。

★ 要落盘就用 `log_target=journal`（默认就是它，等于不用加这个参数）。

## 现在的样子

    PID 1                 systemd
    is-system-running     running
    启动耗时              1.146s (kernel) + 11.096s (userspace)
    yojigen-bootd         active   ← 配网、扩根、hostname、cgroup
    yojigen-sshd          active
    yojigen-docker        active
    1panel-agent/core     active   ← systemd-sysv-generator 自动转的 LSB 脚本
    网络                  10.1.0.7/22（bootd 的 DHCP 配的）
    面板                  HTTP 200

而 `systemctl reboot` 现在也是真的工作。

## 一条白拿的好处

`systemd-sysv-generator` 把 `/etc/init.d/` 里的 LSB 脚本自动变成 service
—— 1Panel 装的那两个**一行没改**就进了 systemd。这正是当初保留 SysV 兼容
骨架（`-Dsysvinit-path=/etc/init.d`）的回报。
