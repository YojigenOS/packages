#!/bin/sh
# docker 的构建步骤 —— 把上游那份静态 tar 装进包里。
#
# 可用的环境变量（由驱动注入）：
#   $SRCDIR   解开的源码根。那个 tar 的第一个成员是 `docker/`，所以 $SRCDIR
#             里直接躺着那几个二进制（不编译任何东西）
#   $PKGDEST  安装目标，等于最终根文件系统里的 /
#   $PKGDIR   本配方目录的副本（files/30-docker 在它下面）
#   $WORK     容器内工作区

#: 上游 29.9.2 那个 tar 里的成员。**写死是有意的** —— 见 package() 里的说明。
BINS="docker dockerd containerd containerd-shim-runc-v2 ctr runc docker-init docker-proxy"

build() {
  : # 没有东西需要编译 —— 这几个二进制是静态的，上游已经建好了
}

package() {
  d="$PKGDEST"

  # ── 1. 二进制 ─────────────────────────────────────────────────────────────
  #
  # ★ 为什么要逐个检查"它在不在"：上游换版本时可能增删成员（比如 27 之后多了
  #   docker-proxy，更早没有）。而 `install` 一个不存在的文件会当场失败 ——
  #   那是好的；真正糟的是【少装一个而没人发现】，然后某个功能在运行时才断
  #   （典型是 containerd-shim-runc-v2 缺了，`docker run` 报的错完全指不到它）。
  #
  #   所以：名单写死、缺一个就失败、并说清是哪个。
  install -d -m 755 "$d/usr/bin"
  for b in $BINS; do
    if [ ! -f "$SRCDIR/$b" ]; then
      echo "✗ 上游 tar 里没有 $b" >&2
      echo "  有的成员：$(ls -1 "$SRCDIR" | tr '\n' ' ')" >&2
      exit 1
    fi
    install -m 755 "$SRCDIR/$b" "$d/usr/bin/$b"
  done

  # ── 2. 目录 ───────────────────────────────────────────────────────────────
  #
  # /var/lib/docker 是 700：那里面是容器的 rootfs，权限松了等于让任何用户
  # 直接读别的容器里的文件。
  install -d -m 755 "$d/etc/docker"
  install -d -m 700 "$d/var/lib/docker"
  install -d -m 755 "$d/var/run"

  # ── 3. 没有 3 ─────────────────────────────────────────────────────────────
  #
  # ★ 这里【曾经】装过一个 `boot.d/30-docker`（起 dockerd 的钩子），2026-10-22 拿掉了。
  #
  #   理由是一条分界：**`boot.d` 是【系统】的启动阶段，不是外部软件的服务表。**
  #
  #     boot.d 该有的    挂 cgroup、设 hostname、扩根分区、控制台键盘 —— 没有它们
  #                      这台机器不是一台能用的机器
  #     docker 属于      一个应用。它没起来，系统照样是系统
  #
  #   而 `20-sshd`（openssh 那个包）犯的是同一个错，也要一起搬 —— 见
  #   `data/yojigen-os-init` 里那条待办。
  #
  # ★★ 所以在服务层做出来之前，docker【不自启】。这是当前诚实的状态：
  #    装了、能手工起（`dockerd &`）、`docker run` 能用，就是开机不会自己起来。
}
