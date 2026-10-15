# shadow 的构建步骤
#
# 只为一件事：**不要把 nologin 装出去**。其余全走默认。

package() {
  # ★ 重定义 package() 就等于把默认那一步接管过来，所以这行不能少。
  #   驱动给 autotools 的默认步骤就是这一条（build.py）。
  make DESTDIR="$PKGDEST" install

  # ── 为什么删 nologin ──────────────────────────────────────────────────
  #
  # **util-linux 也装 /usr/sbin/nologin，路径一模一样。**
  #
  #     shadow      /usr/sbin/nologin   67688 字节
  #     util-linux  /usr/sbin/nologin   67736 字节
  #
  # 两个不同的二进制抢同一个路径，apk 会拒绝装第二个。
  #
  # ★ 这个冲突【还没爆】，因为 shadow 目前不在任何风味里。它是被一次全量扫描
  #   发现的（把所有已建包的产物两两比对路径），而不是被构建或自检抓到的。
  #   一旦 shadow 按计划进 base，它就会爆。
  #
  # ★ 为什么是 shadow 让路，不是 util-linux：
  #
  #     风味         util-linux   shadow
  #     base            ✗           ✗
  #     minimal         ✓           ✗
  #     devel           ✓           ✗
  #
  #   ★ 而这张表 2026-10-15 核过一遍，**`base` 那行原来是错的**（写着 ✓）——
  #     按 `distro/variants/*.toml` 实数是：base 15 个包里既没有 util-linux
  #     也没有 shadow；minimal 20 个和 devel 43 个才有 util-linux。
  #     表格错位也一起修了（`devel` 那行原来和上面两行对不齐）。
  #
  #   **冲突只在 minimal 和 devel 上。** 而那两个风味里都要 util-linux ——
  #   让 util-linux 去掉 nologin，它们当场就没有 nologin 了，那是把一个还没
  #   发生的冲突换成一个立刻发生的功能缺失。
  #
  #   （util-linux 有 --disable-nologin，但那是给它自己用的开关，不是给这里用的。）
  #
  # nologin 的用途是把停用账号的 shell 指到一个「登录被拒绝」的程序上。谁提供
  # 都行，但不能两个都提供。
  rm -f "$PKGDEST/usr/sbin/nologin" \
        "$PKGDEST/usr/share/man/man8/nologin.8"

  # 删干净了没有，当场验——理由同 cpio 那个补丁：静默漏删的表现是【发布时】
  # apk 报一个指不到这里的错。
  for f in usr/sbin/nologin usr/share/man/man8/nologin.8; do
    if [ -e "$PKGDEST/$f" ]; then
      echo "shadow: $f 没删掉——它和 util-linux 撞车，必须删" >&2
      return 1
    fi
  done
}
