# 0026 对标 Fedora Server

**2026-10-25 定。** 它回答的是"我们的基线该照谁的"。

## 起因

YG 问"距离真实可用的系统还差什么"，然后定了三条：

> 1、我希望我们的基础包要全
> 2、我希望我们的包，尽量不要有一些特殊的开关关闭或者功能缺失
> 3、这些包优先进 server 风味，后面我们可以再盘点一波风味清单

而在那之前，对标的是 Debian。**改成 Fedora Server 是因为它有现成的
"风味清单"** —— `comps` 文件把"装什么"分成 158 个组，而 `comps` 是给
安装器 / `dnf group install` 用的**机器可读**定义。

Debian 那边只有 `Priority: required/important/standard` 三个字段，
分散在几千个包的定义里 —— 要自己归纳。

## Fedora 怎么分层（Fedora 43 的 comps，实测拉的）

    core                       44 个   最小可启动
    standard                   74 个   标准系统
    server-product              8 个   Fedora Server 的核心
    server-cfg                  3 个   服务器配置工具
    server-hardware-support    10 个   服务器硬件支持

core + standard 去重后约 **118 个包** —— 这就是"Fedora Server 是什么"。

★ 而 `tasksel` 那个"server 任务"在 Debian bookworm 里**已经删了** ——
  因为它就是 standard。同理 Fedora 的 `server-product` 也只有 8 个包，
  真正的内容在 core + standard 里。

## 我们和它的对照（2026-10-25 第一次盘）

    有对应物    36 个
    缺          47 项

而那 47 项**不能一律补**，分四类：

    【A】该补，纯补配方        14 个
    【B】该补，依赖重           6 个   cryptsetup gnupg2 mdadm
                                      smartmontools ethtool parted
    【C】Fedora/Debian 特有，不该有 11 个  rpm selinux plymouth
                                          dracut initial-setup …
    【D】内核/硬件层，暂时做不了   6 个  audit sssd resolved
                                      microcode_ctl zram fwupd

★ **C 类和 D 类的区分很重要**：前者是"别人的机制"，后者是"我们的边界"。
  把它们混在一起看，会觉得"缺 47 项"很吓人；分完之后剩下该做的就是 A+B。

## 这一轮做的（A 类 + 已有的）

**第 0 期**：仓库里本来就有、只是没进风味的 5 个

    dbus nftables tcpdump perl gettext

**第 1 期**：13 个新配方

    排障    bind9(dig/host/nslookup) pciutils(lspci) usbutils(lsusb) traceroute
    标准    acl attr ed tree whois dos2unix
    前置    libuv liburcu lmdb lzip libusb

★ `bind9` 带出一条 4 层的链（`bind9 → libuv → liburcu` + `lmdb`），
  是我们目前最深的依赖。

## 结果

    server 风味      47 → 94 个包
    /usr/bin 命令    429 → 637 个
    镜像             ~535MB → 1.24GB

## 三条要求怎么落的

### ① 基础包要全

按 Fedora 的 core + standard 逐条对。而"全"的判据是 **Fedora 有、我们也该有**，
不是"我觉得该有"。

### ② 不要有特殊开关关闭

这一轮顺手修了一批**违反这条的既有问题**：

- **`--disable-nls` 从 20 个配方里清掉**（其中 9 个上游真有中文）。
  0012 第 2 条早就禁止它，而那条记录里写的"已经做到了"是假的。
- **`pciutils` 的 `SHARED=0` 改成 `yes`** —— 那个"省一个 .so"的开关实际
  做出了一个装不上自己的包（`so:libpci.so.3 not found`）。
- **`libusb` 不关 `--disable-udev`** —— 关了之后热插拔回调失效，而目标
  系统明明有 libudev（在 systemd 包里）。
- **`bind9` 不显式关任何功能** —— 它的 meson options 全是 `feature: auto`，
  缺依赖就自动关。**不写开关，就没有关掉的东西。**

### ③ 优先进 server 风味

`variants/server.toml` 从 1 个包涨到 16 个（其余 46 继承自 minimal）。

## 剩下的（按优先级）

    第 3 期  打开被关的功能（PAM / kmod / cryptsetup / seccomp）
             ★ 要同时改内核 CONFIG_MODULES=y，风险最高
    第 4 期  B 类的 6 个（cryptsetup gnupg2 mdadm ethtool parted smartmontools）
    第 5 期  风味清单盘点（这个文件之后要单独做）

★ 一处已知欠账：`lspci -k`（显示驱动名）要 `kmod`，现在缺。

## 一条方法论

**对标要用机器可读的定义**，不是看别人的文档说"装了这些"。

`comps.xml` 是 Fedora 给安装器用的，里面的 `packagelist` 就是这个组
**真的装什么**。而文档里的清单往往是"典型安装"，会漏。

★ 这一条和 0025 里那条"判据要站在离成因最近的地方"是同一件事：
  看源数据，不看关于源数据的描述。
