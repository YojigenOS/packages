# e2fsprogs 的构建步骤
#
# 上游的 configure/make 就够用，这里只做一件事：**让出那些不归我们的文件**——
# fsck，以及它自带的那份 libblkid / libuuid。

package() {
    # ★ 重定义 package() 就等于把默认那一步接管过来，所以这行不能少。
    #   驱动给 autotools 的默认步骤就是这一条（build.py:158）。
    #
    #   我第一版【没有】这一行，于是进入包里的东西一个都没有——而报出来的错是
    #   "找不到 resize2fs"，看起来像路径问题，实际是根本没装。
    #   cpio 的 package() 开头就写着这条，我上次只读了它下面 rm -f 那段。
    make DESTDIR="$PKGDEST" install
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

    # ── 为什么也删自带的那份 libblkid / libuuid ───────────────────────────
    #
    # 和 fsck 是同一个问题的【另一批文件】，只是它到 2026-10-13 才第一次现形：
    # 那天 aarch64 第一次真的去装 minimal 风味，而 minimal 里【同时】有
    # e2fsprogs（yojigen-os-init 的运行时依赖）和 util-linux。apk 当场报：
    #
    #     ERROR: e2fsprogs-1.47.4-r1: trying to overwrite usr/sbin/blkid
    #         owned by util-linux-2.42.4-r1.
    #
    # 一共 25 个文件（blkid / findfs / uuidgen / libblkid.a / libuuid.a /
    # blkid.h / uuid.h / blkid.pc / uuid.pc / 一堆 man 页）。
    #
    # ★ 它们该归 util-linux —— 这不是偏好，是【上游的事实】：
    #   libblkid 和 libuuid 本来就是 util-linux 的源码树里的库。e2fsprogs 那份
    #   是 2007 年前后拷进去的私有副本，只为"系统里没有 util-linux"的场合留着。
    #   Debian / Fedora / Alpine 建 e2fsprogs 时都传 --disable-libuuid
    #   --disable-libblkid，用的就是 util-linux 那份。
    #
    # 所以按项目的规矩让路（tar/cpio 抢 rmt → cpio 让；shadow/util-linux 抢
    # nologin → shadow 让）：**谁的范围，归谁。**
    #
    # ★★ 但为什么这里是 rm 而不是 --disable-libuuid / --disable-libblkid。
    #
    # 那是我原本想要的写法，试过，用不了：e2fsprogs 的 configure.ac 里，
    # `--disable-libuuid` 走的是"**用系统那份**"那条路，而那条路第一步就是
    #
    #     if test -z "$PKG_CONFIG"; then
    #         AC_MSG_ERROR([pkg-config not installed; please install it.])
    #     fi
    #
    # ——它要 pkg-config 这个【程序】。而 aarch64 的 devel 镜像里没有它
    # （镜像建于 10-11，pkgconf 是之后才加进 distro 的 devel.toml 的；
    # x86_64 的镜像重建过，所以有）。这个缺口是 distro 那边的，配方治不了。
    #
    # ★★★ 顺带查出来一件更要紧的事，它才是这批文件忽有忽无的原因：
    #
    # 那两个开关【什么都不写】时，configure 的行为取决于环境里有没有
    # pkg-config：有就自动改用系统的库，没有就退回自带那份。于是
    #
    #     x86_64（镜像里有 pkg-config）  → 不自带 → 96 个文件，谁也不撞
    #     aarch64（镜像里没有）          → 自带   → 143 个文件，撞 25 个
    #
    # 同一个配方、同一份源码，两个架构产出【不同的包】——而且差别不在明面上，
    # 在"构建环境里恰好有没有某个程序"这种地方。这正是决策记录 0011 说的
    # 隐式依赖：它不报错，只是让产物悄悄分叉。
    #
    # 所以 spec.toml 那边把开关【显式】写成 --enable-libuuid --enable-libblkid：
    # 明确要自带的那份（依赖最少，这是原作者的本意），两个架构从此编出同一套
    # 东西；然后再在这里把不归我们的文件让出去。等 aarch64 的镜像重建、有了
    # pkg-config，可以再考虑反过来换成 --disable-*，那是另一件事。
    rm -f "$PKGDEST/usr/bin/uuidgen" \
          "$PKGDEST/usr/sbin/blkid" \
          "$PKGDEST/usr/sbin/findfs" \
          "$PKGDEST/usr/lib/libblkid.a" \
          "$PKGDEST/usr/lib/libuuid.a" \
          "$PKGDEST/usr/lib/pkgconfig/blkid.pc" \
          "$PKGDEST/usr/lib/pkgconfig/uuid.pc" \
          "$PKGDEST/usr/share/man/man1/uuidgen.1" \
          "$PKGDEST/usr/share/man/man3/libblkid.3" \
          "$PKGDEST/usr/share/man/man8/blkid.8" \
          "$PKGDEST/usr/share/man/man8/findfs.8"
    rm -f "$PKGDEST"/usr/share/man/man3/uuid*.3     # uuid(3) 加 12 个别名页
    rm -rf "$PKGDEST/usr/include/blkid" "$PKGDEST/usr/include/uuid"

    # ★ 这次不用一张手写清单去验，改成按【类别】扫一遍。
    #
    # 上面 25 个文件是照着产物列出来的，而"照着列出来的清单"最容易漏一个，
    # 漏掉的表现又是发布之后才响的 apk 冲突。所以断言写成"这个包里不该再有
    # blkid / uuid / findfs 的任何文件"——它不依赖我记得全不全。
    left=$(find "$PKGDEST" \( -name 'libblkid*' -o -name 'libuuid*' -o -name 'blkid*' \
                              -o -name 'uuid*' -o -name 'findfs*' \) 2>/dev/null)
    if [ -n "$left" ]; then
        echo "  ✗ 还有属于 util-linux 的文件留在 e2fsprogs 里：" >&2
        printf '%s\n' "$left" | sed 's/^/      /' >&2
        echo "    （util-linux 装着的时候 apk 会拒绝安装）" >&2
        exit 1
    fi

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
            echo "  ✗ 找不到 ${f}——首启扩盘（resize2fs）和开机 fsck（e2fsck）都指着它" >&2
            echo "    在 \$PKGDEST 下扫了一遍：$(find "$PKGDEST" -name "$f" 2>/dev/null | head -3)" >&2
            exit 1
        fi
        echo "  · $found"
    done
}
