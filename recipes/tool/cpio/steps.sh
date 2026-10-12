# cpio 的构建步骤
#
# 只为一件事：**不要把 rmt 装出去**。

package() {
  # ★ 重定义 package() 就等于把默认那一步接管过来，所以这行不能少。
  #   驱动给 autotools 的默认步骤就是这一条（build.py）。
  make DESTDIR="$PKGDEST" install

  # ── 为什么删 rmt ──────────────────────────────────────────────────────
  #
  # **tar 也装这两个文件，路径一模一样。** 两个包同时进一个系统时 apk 拒绝：
  #
  #     ERROR: tar-1.35-r1: trying to overwrite usr/libexec/rmt
  #            owned by cpio-2.15-r1.
  #     ERROR: tar-1.35-r1: trying to overwrite usr/share/man/man8/rmt.8
  #            owned by cpio-2.15-r1.
  #     2 errors; 906.9 MiB in 49 packages
  #
  # 这是【发布前的自检】在 x86_64 上抓到的。它装的是 base-devel 那 49 个包，
  # 而 tar 和 cpio 都在里面。
  #
  # rmt 是「remote magnetic tape」——让 tar/cpio 通过 rsh 读写远端磁带机的
  # 小程序。2026 年没人这么用。两个包各带一份纯粹的重复。
  #
  # ★ 为什么是 cpio 让路，不是 tar：**Alpine 就是这么分的**。实测：
  #
  #     $ apk info -L tar   → usr/libexec/rmt
  #     $ apk info -L cpio  → （没有 rmt）
  #
  # 上游给不了答案——GNU 的 cpio 把 rmt 列在 SUBDIRS 里【无条件】构建：
  #
  #     Makefile.am:21: SUBDIRS = doc gnu lib rmt src po tests
  #
  # 它有个 --enable-mt 开关，但那个管的是 mt（磁带控制程序），不是 rmt ✗。
  # 所以只能装完再删。
  rm -f "$PKGDEST/usr/libexec/rmt" \
        "$PKGDEST/usr/share/man/man8/rmt.8"

  # 删干净了没有，当场验——不留"下次谁记得检查"。
  #
  # 理由和 openssh 那段一样：静默地少删一个文件，表现是【发布时】apk 报一个
  # 指不到这里的错。在这里响亮地失败，原因就在眼前。
  for f in usr/libexec/rmt usr/share/man/man8/rmt.8; do
    if [ -e "$PKGDEST/$f" ]; then
      echo "cpio: $f 没删掉——它和 tar 撞车，必须删" >&2
      return 1
    fi
  done
}
