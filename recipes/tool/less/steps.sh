# less 的构建步骤
#
# 三步，顺序不能换：
#
#   1. make -f Makefile.aut     生成 funcs.h / help.c / lessmsg.inc / configure
#   2. ./configure
#   3. make
#
# ★ 第一步是从 git 建【专有】的。上游 README 第二节写着它，而发布的
#   tarball 里那些文件是现成的、不需要这一步——所以只看 Makefile.in
#   是看不出来的。

build() {
    cd "$SRCDIR"

    # 1. 生成那些"发布包里才有"的文件。
    #
    #    ★ 要【点名】几个目标，不能整个 distfiles。
    #
    #      这一段撞了三回，每一回都往前挪一步：
    #
    #        1. 跑 `make -f Makefile.aut`（= all）
    #           → install 报 No rule to make target 'less.nro'
    #             （all 只生成"能编"的，distfiles 才生成"能装"的）
    #
    #        2. 跑 `make -f Makefile.aut distfiles`
    #           → 它顺手要生成 less.man，而那要 nroff：
    #                nroff -t -man ./less.nro >./less.man
    #                /bin/sh: 1: nroff: not found
    #             nroff 是 groff 的一部分。为了【一个手册页】把 groff
    #             整个拉进来不值得。
    #
    #        3. 点名要的那几个 ✓
    #
    #      "编过了"和"装得上"是两件事——这个包把这句话演示了三遍。
    #    ★ 两行，不是一行：
    #
    #      `all`      生成 help.c / funcs.h / lessmsg.inc / configure
    #      `less.nro` 单独点名 —— 它是从 less.nro.VER 模板生出来的
    #                 （Makefile.aut:  ${srcdir}/less.nro: ${srcdir}/less.nro.VER）
    #                 而 `all` 里【没有它】，`distfiles` 里有但要顺带生成
    #                 less.man，那一步要 nroff（= groff）。
    #
    #    ★ 注意这一段的形状：我连着改了三回，每一回都是**照着报错修**——
    #      "No rule to make target 'less.nro'" → 去点名 less.nro；
    #      "nroff: not found" → 不要整个 distfiles。
    #      最后去看 Makefile 里那条【真实的规则】，才知道它从哪来。
    #      **读一遍上游的构建文件，比照着报错改三遍快。**
    make -f Makefile.aut all

    #    ★ 手册页那三个，从模板【推导】出来，不要一个一个点名。
    #
    #      它们的规则长这样（Makefile.aut）：
    #          ${srcdir}/less.nro:     ${srcdir}/less.nro.VER     ${srcdir}/version.c
    #              ${REPLACE_VERSION} ${srcdir}/less.nro.VER
    #      ——每个 .nro 都从对应的 .nro.VER 模板替换版本号生出来。
    #
    #      我一开始点名了 less.nro，然后又撞上 lesskey.nro；写死第二个就还会
    #      撞第三个（DISTFILES 里那一串是 less/lesskey/lessecho 三对）。
    #
    #      **点名 = 每次上游加一个就要再来一次。推导 = 不用管。**
    for t in *.nro.VER; do
        [ -e "$t" ] || continue
        make -f Makefile.aut "${t%.VER}"
    done

    # 2. 确认第一步真的产出了我们要的那两个。
    #    不验的话，失败会延后到编译时，报一句和成因隔着好几层的
    #    "No rule to make target 'funcs.h'"。
    #    验的是"配得成"和"装得上"各要的那几个。
    for f in funcs.h lessmsg.inc configure less.nro lesskey.nro lessecho.nro; do
        [ -e "$f" ] || { echo "  ✗ make -f Makefile.aut 之后 $f 还是不在"; exit 1; }
    done
    echo "  ✓ funcs.h / lessmsg.inc / configure 都生成了"

    # 3. 正常那两步。
    ./configure --prefix=/usr --sysconfdir=/etc
    make
}

package() {
    cd "$SRCDIR"
    make DESTDIR="$PKGDEST" install
    [ -x "$PKGDEST/usr/bin/less" ] || { echo "  ✗ /usr/bin/less 不在"; exit 1; }
    echo "  ✓ less 装好了"
}
