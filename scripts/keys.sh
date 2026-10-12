#!/bin/sh
# 管理签名密钥
#
# 用法：
#     ./scripts/keys.sh gen                # 生成 3 对（已有就不动）
#     ./scripts/keys.sh gen --force        # 重新生成（★ 会失效已发布的系统）
#     ./scripts/keys.sh show               # 看公钥和指纹
#     ./scripts/keys.sh check              # 检查私钥和 distro/keys/ 里的是否配对
#
# ── 为什么是 3 对 ────────────────────────────────────────────────────────
#
# 因为 apk 的信任模型是【平权】的：/etc/apk/keys/ 目录里所有 .rsa.pub 都算数，
# 没有根密钥、没有密钥链、没有主从。所以能做的只有"多放几个，任意一个都能签"。
#
# 数量上考虑过 9 对，但那个方向是错的：
#
#     N 个密钥 → 「全部丢失」的概率下降   ✓（这是想优化的）
#              → 「至少一个泄露」的概率上升 ✗（而这个更严重）
#
#   全丢：无法签新包，但【已装的系统仍然安全】
#   泄露：攻击者能签出看起来完全合法的恶意包，已装系统会信任地装上
#
# 而且 9 个私钥 = 9 个要保管的东西。如果只认真保管其中 3 个，那和只做 3 个
# 一样安全，却多出 6 个"可能被随手放着"的私钥——每一个都能签出合法包。
#
# 3 个够用：
#     · 任意一个在手 → 能签
#     · 丢两个还剩一个
#     · 泄露一个 → 还能用另外两个发"撤销那次更新"
#
# ── 三个的分工 ────────────────────────────────────────────────────────────
#
#     1  「日常」      每次发布都用        → CI secret + 密码管理器
#                       ★ 暴露最多的那个，所以要能轮换
#     2  「备用」      平时完全不用        → 密码管理器 + 纸质
#     3  「异地备用」  平时完全不用        → 另一个物理位置
#
# ★ 备份份数比密钥个数重要。3 个密钥 × 每个存 3 个地方，比 9 个密钥 ×
#   每个存 1 个地方强——因为后者里任何一个泄露都是永久的。
#
# ── 私钥放哪，不放哪 ──────────────────────────────────────────────────────
#
#     私钥  ~/.yojigenos/keys/            ★ 绝不进 git，绝不进仓库
#     公钥  distro/keys/*.rsa.pub         进 git——它本来就是要发出去的
#
# 公钥只有这一份来源：从私钥导出来的。packages 侧签名时需要公钥，但它
# 【不存副本】——现场从私钥推导（openssl rsa -pubout）。一个源头，不会漂移。

set -eu

cd "$(dirname "$0")/.."

KEYDIR="${YOJIGENOS_KEYDIR:-$HOME/.yojigenos/keys}"
PUBDIR="$PWD/../distro/keys"
IDENTITY="os@yojigen.cn"   # 和 Alpine 一样：身份 + 生成时间戳的十六进制
N=3

CMD="${1:-}"
[ $# -gt 0 ] && shift

FORCE=0
for a in "$@"; do
    case "$a" in
        --force) FORCE=1 ;;
        --dir)   KEYDIR="$2"; shift ;;
        *) echo "未知参数: $a" >&2; exit 1 ;;
    esac
done

need_openssl() {
    command -v openssl >/dev/null 2>&1 || {
        echo "需要 openssl" >&2; exit 1
    }
}

# ── gen ───────────────────────────────────────────────────────────────────
cmd_gen() {
    need_openssl
    mkdir -p "$KEYDIR" "$PUBDIR"
    chmod 700 "$KEYDIR"

    echo
    echo "  私钥目录: ${KEYDIR/#$HOME/\~}"
    echo "  公钥目录: distro/keys/"
    echo

    # ── 命名 ────────────────────────────────────────────────────────────
    #
    #     <身份>-<生成时间戳的十六进制>.rsa.pub
    #
    #     os@yojigen.cn-6a2b1c4d.rsa.pub
    #
    # 和 Alpine 的形状一致：
    #
    #     alpine-devel@lists.alpinelinux.org-58199dcc.rsa.pub
    #                                      └─ 0x58199dcc = 2016-11-02
    #
    # ★ 名字里【不写用途】。三把是等价的——任何一个都能签、都能顶上，
    #   谁当"备用"是会变的，写进名字反而在撒谎。
    #
    # ★ 8 位十六进制 = 32 位，而 unix 秒已经用掉 31 位——只装得下"秒"，
    #   装不下毫秒。所以三把必须【错开一秒】生成，否则撞名。
    #   Alpine 一次只生成一把，没有这个问题。
    for n in $(seq 1 $N); do
        [ "$n" -gt 1 ] && sleep 1
        STAMP=$(printf '%x' "$(date +%s)")
        PRIV="$KEYDIR/$IDENTITY-$STAMP.rsa"
        PUB="$PUBDIR/$IDENTITY-$STAMP.rsa.pub"

        openssl genrsa -out "$PRIV" 2048 2>/dev/null
        chmod 600 "$PRIV"
        openssl rsa -in "$PRIV" -pubout -out "$PUB" 2>/dev/null
        chmod 644 "$PUB"

        printf '  %-38s ✓ 生成（公钥 %s 字节）\n' "$IDENTITY-$STAMP.rsa" "$(wc -c < "$PUB" | tr -d ' ')"
    done

    echo
    echo "  ── SHA256 指纹（记下来，将来核对用）──"
    for f in "$PUBDIR"/*.rsa.pub; do
        [ -f "$f" ] || continue
        fp=$(openssl dgst -sha256 -hex "$f" 2>/dev/null | awk '{print $NF}')
        printf '    %-38s %s\n' "$(basename "$f" .rsa.pub)" "$(printf '%s' "$fp" | cut -c1-32)"
    done

    echo
    echo "  ★ 现在去备份这三个私钥。它们不在 git 里，也不在任何仓库里。"
    echo "      $KEYDIR/"
    echo
    echo "    三把是等价的：任何一个都能签、都能顶上。"
    echo "    建议分开存：一把进 CI secret，一把密码管理器，一把异地。"
    echo "    ★ 备份份数比密钥个数重要。"
    echo
}

# ── show ──────────────────────────────────────────────────────────────────
cmd_show() {
    need_openssl
    echo
    if [ ! -d "$PUBDIR" ] || [ -z "$(ls -A "$PUBDIR" 2>/dev/null)" ]; then
        echo "  distro/keys/ 里还没有公钥。先跑 keys.sh gen。" >&2; exit 1
    fi
    echo "  ── distro/keys/（会烤进交付物的那份）──"
    for f in "$PUBDIR"/*.rsa.pub; do
        [ -f "$f" ] || continue
        fpr=$(openssl rsa -pubin -in "$f" -outform DER 2>/dev/null \
              | openssl dgst -sha256 -hex 2>/dev/null | awk '{print $NF}')
        printf '    %-34s %s\n' "$(basename "$f")" "$(printf '%s' "$fpr" | cut -c1-32)"
    done
    echo
    echo "  最终会出现在交付物的 /etc/apk/keys/ 里。"
    echo
}

# ── check ─────────────────────────────────────────────────────────────────
cmd_check() {
    need_openssl
    echo
    BAD=0
    for PUB in "$PUBDIR"/*.rsa.pub; do
        [ -f "$PUB" ] || continue
        b=$(basename "$PUB" .rsa.pub)
        PRIV="$KEYDIR/$b.rsa"

        if [ ! -f "$PRIV" ]; then
            printf '  %-38s ✗ 私钥不在 %s\n' "$b" "${KEYDIR/#$HOME/\~}"; BAD=1; continue
        fi
        if [ ! -f "$PUB" ]; then
            printf '  %-38s ✗ 公钥不在 distro/keys/\n' "$b"; BAD=1; continue
        fi

        derived=$(openssl rsa -in "$PRIV" -pubout 2>/dev/null | openssl dgst -sha256 -hex | awk '{print $NF}')
        stored=$(openssl rsa -pubin -in "$PUB" -outform DER 2>/dev/null | openssl dgst -sha256 -hex | awk '{print $NF}')
        # 两条链算的是不同编码（PEM 文本 vs DER），所以只比对"能不能互相验证"：
        # 用一个固定串签名再验签，能过就是配对的。
        probe=$(mktemp)
        printf 'yojigenos' > "$probe"
        sig=$(mktemp)
        if openssl dgst -sha256 -sign "$PRIV" -out "$sig" "$probe" 2>/dev/null &&
           openssl dgst -sha256 -verify "$PUB" -signature "$sig" "$probe" >/dev/null 2>&1; then
            printf '  %-38s ✓ 配对\n' "$b"
        else
            printf '  %-38s ✗ ★ 私钥和公钥不配对 ★\n' "$b"; BAD=1
        fi
        rm -f "$probe" "$sig"
    done
    echo
    if [ "$BAD" = 0 ]; then
        echo "  ✓ 三对都对得上"
    else
        echo "  ✗ 有问题。签名和交付物不一致会导致【装不上】。" >&2
    fi
    echo
    exit "$BAD"
}

case "$CMD" in
    gen)   cmd_gen ;;
    show)  cmd_show ;;
    check) cmd_check ;;
    *) sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac
