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

  # ── 第 2 件事：那 6 份 pam.d 归 linux-pam，不归 shadow（2026-10-26）──────
  #
  # ★★★ 上游 `etc/pam.d/Makefile.am` 里有：
  #
  #         pamd_files = chpasswd chfn chsh login newusers passwd
  #         pamddir = $(sysconfdir)/pam.d
  #         pamd_DATA = $(pamd_files)
  #
  #   而 `make install` 把它们**无条件**装到 /etc/pam.d/（不检查已存在）。
  #   于是装 shadow 时：
  #
  #       ERROR: shadow-4.20.3-r2: trying to overwrite etc/pam.d/su
  #              owned by linux-pam-1.7.3-r5.
  #
  # ★★ 而两份内容不一样，那个差别是决定性的：
  #
  #       shadow 的 su:     auth required pam_wheel.so use_uid   ← 只有 wheel 组能用
  #       linux-pam 的 su:  # auth required pam_wheel.so …        ← 注释掉（Fedora 默认）
  #
  #   而我们的 root **不在 wheel 组**（`id root` → `groups=0(root)`）——
  #   shadow 那份装上去之后，**root 自己 su 都会失败**。
  #
  # ★ 所以归属这样划：
  #     linux-pam 拥有 su / login / passwd / chfn / chpasswd / chsh
  #                —— 它是"认证栈"这件事的所有者
  #     shadow    只留 newusers
  #                —— newusers 是 shadow 独有的工具，只属于它
  #
  #   ★ 不是"绕过检查"：标准发行版里这几个文件的归属也是发行版自己划的
  #     （Debian 是 libpam-runtime + 各应用自己带；Fedora 是 authselect 统一
  #     生成）。**关键是"谁拥有"有一个答案，并且写下来。**
  for f in su login passwd chfn chpasswd chsh; do
    rm -f "$PKGDEST/etc/pam.d/$f"
  done
  # newusers 要留着，删过头同样是错
  for f in etc/pam.d/newusers; do
    if [ ! -e "$PKGDEST/$f" ]; then
      echo "shadow: $f 不见了——那是 shadow 独有的，该留着" >&2
      return 1
    fi
  done
}
