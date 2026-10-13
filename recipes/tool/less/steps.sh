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
    #    它的 all 目标就是干这个的（注释原文：Make files needed to run
    #    Makefile (after configure)）。
    make -f Makefile.aut

    # 2. 确认第一步真的产出了我们要的那两个。
    #    不验的话，失败会延后到编译时，报一句和成因隔着好几层的
    #    "No rule to make target 'funcs.h'"。
    for f in funcs.h lessmsg.inc configure; do
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
