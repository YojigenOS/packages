#!/bin/sh
# iptables —— 装完之后把「默认名字」指向 nft 后端。
#
# ── 为什么必须这么做 ──────────────────────────────────────────────────────
#
# `--enable-nftables` 只保证 `iptables-nft` 那个程序**存在**。而 `ip t a b l e s`
# 这个名字（以及 iptables-save / iptables-restore）在 [u]pstream 的 make install
# 里**一律指向 legacy**：
#
#     /usr/sbin/iptables        -> xtables-legacy-multi
#     /usr/sbin/iptables-nft    -> xtables-nft-multi
#
# 而我们的内核【没有 legacy 那套表】—— `/proc/net/ip_tables_names` 是空的，
# `iptables -L` 报 `can't initialize iptables table 'filter': Table does not exist`。
# 于是 dockerd 一 exec `iptables` 就失败，它的报错是：
#
#     failed to register "bridge" driver: failed to create NAT chain
#     Perhaps iptables or your kernel needs to be upgraded.
#
# ★ 所以这里把三个默认名字改指 nft。**这是这台发行版的正确配置，不是权宜之计**：
#   上游那个默认值假设"内核两套表都在"，而我们只有 nft 那套。
#
# ★★ 而 Debian 之类用 `update-alternatives` 管这件事 —— 我们没有那个机制，
#    也不需要：这里只有一个正确的答案。

package() {
  make DESTDIR="$PKGDEST" install

  for n in iptables iptables-save iptables-restore; do
    if [ -L "$PKGDEST/usr/sbin/$n" ] || [ -f "$PKGDEST/usr/sbin/$n" ]; then
      ln -sf xtables-nft-multi "$PKGDEST/usr/sbin/$n"
    fi
  done
}
