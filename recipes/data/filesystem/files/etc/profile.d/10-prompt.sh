# 命令提示符。
#
# 【为什么需要它】
#
# 不设 PS1 的话，bash 用它自己的出厂默认：
#
#     \s-\v\$
#
# 于是 SSH 上去看到的是：
#
#     -bash-5.3#
#     ↑    ↑   ↑
#     │    │   └ 版本
#     │    └───── shell 名
#     └────────── login shell 的前导减号（\s 在 login shell 下带它）
#
# 那句话不算错，但它是**没人设过**的痕迹。别的发行版都有人设：
#
#     Debian   /etc/bash.bashrc 里  PS1='\u@\h:\w\$ '
#     RHEL     /etc/bashrc
#     Alpine   /etc/profile 里     export PS1='\h:\w\$ '
#
# 我们原来谁都没设。而 /etc/profile 里那个 profile.d 的循环一直空转着——
# **机制在，没人往里放东西。**
#
# 【格式】
#
#     \u  用户名        \h  主机名（短）
#     \w  当前目录（家目录显示成 ~）   \$  root 是 #，普通用户是 $
#
# → root@YojigenOS:~#
#
# 【为什么放在 profile.d 而不是 /etc/bashrc】
#
# /etc/profile 是登录 shell 读的，而它的 profile.d 循环是【所有 POSIX shell】
# 都会走的（bash、dash、sh 都是）。放在这儿，换个 shell 也有提示符。
#
# SSH 登录就是登录 shell（提示符里那个前导减号正说明这一点），所以这条覆盖
# 了最常见的场景。
#
# ★ 这里不用 ${VAR} 那种花括号写法不是因为不需要——是因为**没有变量名紧跟中文**。
#   PS1 里的 \u \h 是【提示符转义】，不是 shell 变量，不会被展开成空。

PS1='\u@\h:\w\$ '
export PS1
