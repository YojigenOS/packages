# e2fsprogs 的构建步骤
#
# 上游的 configure/make 就够用，这里只做一件事：**让出两个不归我们的文件**。

package() {
    # default 的 install 已经跑完了（autotools 类型会做），这里只做删减。
    #
    # ── 为什么删 fsck ─────────────────────────────────────────────────────────
    #
    # `/usr/sbin/fsck` 是个**转发脚本**：它按 /etc/fstab 里那一行的 type 字段，
    # 找到真正的检查程序（ext4 → e2fsck，xfs → xfs_repair，vfat → fsck.vfat…）
    # 再调过去。所以它天然属于 util-linux ——那个包管的是"按 fstab 干活"这件事，
    # 而不是某一种文件系统。
    #
    # 两个包都提供它，装的时候 apk 会直接报错：
    #
    #     ERROR: e2fsprogs-1.47.4-r1: trying to overwrite usr/sbin/fsck
    #         owned by util-linux-2.42.4-r1.
    #
    # ★ Alpine 也是这么分的（实测）：
    #     /sbin/fsck      → util-linux-misc
    #     /sbin/e2fsck    → e2fsprogs
    #     /sbin/resize2fs → e2fsprogs
    #
    # 和 tar/cpio 抢 rmt、shadow/util-linux 抢 nologin 是同一类问题：
    # **两个包都提供了同一件东西，而它只能有一个主人。**
    # 怎么选的标准是"这个文件属于谁管的范围"，不是"谁先做的"。
    rm -f "$PKGDEST/usr/sbin/fsck" \
          "$PKGDEST/usr/share/man/man8/fsck.8"

    # 删干净了没有，当场验——理由同 cpio 那个补丁：静默漏删的表现是【发布时】
    # apk 报一个指不到这里的错（"trying to overwrite"），而那时已经换了一层了。
    for f in "$PKGDEST/usr/sbin/fsck" "$PKGDEST/usr/share/man/man8/fsck.8"; do
        if [ -e "$f" ]; then
            echo "  ✗ $f 没删掉——util-linux 那边装着的时候 apk 会拒绝安装" >&2
            exit 1
        fi
    done

    # 我们要的东西必须在。
    #
    # ★ 不假设路径。`--prefix=/usr` 不决定 sbindir——e2fsprogs 有单独的
    #   --sbindir，装到 /sbin 还是 /usr/sbin 取决于它自己的默认值。
    #   我第一版写死了 "$PKGDEST/usr/sbin/resize2fs"，于是这个检查在
    #   【文件其实好好的】情况下报"不在"——而它报得对：错的是我的假设。
    #
    #   所以：两个位置都找，找到了就把真实位置打出来。日志里有这个信息，
    #   下一次别人问"到底装哪了"就不用再翻上游的 configure。
    for f in resize2fs e2fsck; do
        found=""
        for d in usr/sbin sbin usr/bin bin; do
            if [ -e "$PKGDEST/$d/$f" ]; then
                found="$d/$f"
                break
            fi
        done
        if [ -z "$found" ]; then
            echo "  ✗ 找不到 $f——首启扩盘（resize2fs）和开机 fsck（e2fsck）都指着它" >&2
            echo "    在 \$PKGDEST 下扫了一遍：$(find "$PKGDEST" -name "$f" 2>/dev/null | head -3)" >&2
            exit 1
        fi
        echo "  · $found"
    done
}
