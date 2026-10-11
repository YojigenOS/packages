# openssl 的构建步骤
#
# openssl 有一套自己的 Configure——注意是大写 C，它和 autotools 的 configure
# 毫无关系，参数也完全不同。所以这个包是 custom 类型。

build() {
    # ── 目标平台要显式给 ──────────────────────────────────────────────
    #
    # openssl 的 ./config 会自动探测，但那让产物取决于【在哪台机器上构建】。
    # 显式写出来，构建就只由配方决定。（这一路已经栽过一次同类的坑：gawk 的
    # configure 自动探测到 mpfr 就链接它，于是同一个配方在两种环境里产出两种
    # gawk。见那个包的注释。）
    case "$(uname -m)" in
        aarch64) TARGET=linux-aarch64 ;;
        x86_64)  TARGET=linux-x86_64  ;;
        *) echo "不认识的架构: $(uname -m)" >&2; return 1 ;;
    esac

    ./Configure \
        "$TARGET" \
        --prefix=/usr \
        --openssldir=/etc/ssl \
        --libdir=lib \
        shared \
        no-tests

    # --libdir=lib 不是可有可无：有些目标下 openssl 默认装到 lib64，而我们的
    # 是 usr-merge 布局，库统一在 /usr/lib（决策记录 0001）。写死它，免得
    # 不同架构产出不同的目录结构。
    #
    # no-tests 跳过它自带的测试程序，省时间；测试是另一回事，不该混在构建里。
    # （决策记录 0010 说上游测试要跑，但那是单独一步，不是这里。）

    make
}

package() {
    # install_sw = 只装软件，不装文档。man 手册和 HTML 文档对构建环境没用，
    # 白占体积。
    make install_sw DESTDIR="$PKGDEST"

    # openssl 的 install_sw 会把 /etc/ssl 下的配置文件也装进去（openssl.cnf）。
    # 那是运行时需要的，保留。
}
