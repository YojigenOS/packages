#!/bin/bash
# 把一个架构的所有 .apk 收成一个 apk 软件仓库
#
# 用法：
#     ./scripts/repo.sh                    # aarch64
#     ./scripts/repo.sh --arch x86_64
#     ./scripts/repo.sh --sign-key ~/.yojigenos/yojigenos.rsa
#
# 产出：
#     var/repo/<arch>/APKINDEX.tar.gz      ← 索引（唯一的"特殊"文件）
#     var/repo/<arch>/*.apk                ← 所有已发布的包
#
# ── 一个 apk 仓库【不是一个服务】────────────────────────────────────────
#
# 它就是【一个目录 + 一个索引文件】。没有数据库、没有要跑的程序、没有 API。
# 所以任何能发静态文件的托管都行：nginx / Cloudflare R2 / S3 / 普通 web 目录。
#
#     repo.os.yojigen.cn/
#     ├── aarch64/
#     │   ├── APKINDEX.tar.gz
#     │   ├── hello-2.12.3-r1.apk
#     │   └── …（每个包每个版本一份）
#     └── x86_64/
#         └── 同上
#
# ── 仓库 URL 和 pkgname-spec 的配合（踩过）────────────────────────────────
#
# 客户端那边的写法是【仓库根】，apk 自己会加架构那层：
#
#     --repository https://repo.os.yojigen.cn
#       → 它去拿 https://repo.os.yojigen.cn/aarch64/APKINDEX.tar.gz
#
# 所以 --pkgname-spec 要【带】架构前缀——它是相对于仓库根的：
#
#     --pkgname-spec 'aarch64/${name}-${version}.apk'
#
# 两边都带就变成 aarch64/aarch64/… （实测 404），两边都不带也拿不到包。
#
# ── 为什么不用 sync --delete ──────────────────────────────────────────────
#
# 包仓库是【只增不减】的：manifest 钉住版本，半年后还要能按同样的版本重建。
# 所以上传用 `rclone copy`，不能用 `rclone sync`（那会删掉旧版本）。

set -eu

cd "$(dirname "$0")/.."

ARCH=aarch64
SIGN_KEY="${SIGN_KEY:-}"
DESC=""

while [ $# -gt 0 ]; do
    case "$1" in
        --arch)     ARCH="$2"; shift 2 ;;
        --sign-key) SIGN_KEY="$2"; shift 2 ;;
        --desc)     DESC="$2"; shift 2 ;;
        -h|--help)  sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "未知参数: $1" >&2; exit 1 ;;
    esac
done

PKGS="$PWD/var/pkgs/$ARCH"
REPO="$PWD/var/repo/$ARCH"

[ -d "$PKGS" ] || { echo "还没有包。先跑 scripts/pack.sh。" >&2; exit 1; }

COUNT=$(ls "$PKGS"/*.apk 2>/dev/null | wc -l | tr -d ' ')
[ "$COUNT" -gt 0 ] || { echo "$PKGS 里没有 .apk" >&2; exit 1; }

echo
echo "  收拢 $COUNT 个包 → var/repo/$ARCH/"
mkdir -p "$REPO"

# ── 先把【线上已有的包】拉回来 ────────────────────────────────────────────
#
# ★ 为什么必须做。
#
# CI 每次都是干净的工作区，var/pkgs 里只有【这次打的包】。直接 mkndx 的话，
# 生成的索引只列这次的包——传上去就会把之前发布的包从索引里抹掉。
#
# 包文件还在线上，但 apk 只能看到索引里列的包，所以它们等于消失了。
#
# ★ 而且 apk 没有别的办法：mkndx 的 --index 只是"复用元数据缓存来加速"，
#   实测它连已有索引里的条目都认不出来（建一个 2 项的索引，用 --index 读它，
#   报的是 "0 packages"）。它也没有"往索引里追加"的子命令。
#
# 所以只能把包文件凑齐——索引是从包文件生成的，就绕不开这一步。
#
# ★ 只拉 .apk，不拉索引：索引要重新生成（它得包含这次的包）。
#
# ★ 默认就做，不设开关。原来没有这一步，是因为"CI 是干净的"这件事被忽略了；
#   做成可选的话，忘了加开关就又是一次事故。
#   没有凭据（本地开发）时自动跳过。
SYNCED=0
if [ -n "${S3_BASE_URL:-}" ] && [ -n "${S3_BUCKET:-}" ] && [ -n "${S3_AK:-}" ]; then
    echo
    echo "  拉回线上已有的包…"
    # shellcheck disable=SC2086
    #
    # ★ --user：rclone 必须以【宿主的 uid/gid】跑。
    #
    # 否则它拉下来的文件属主是 root，而调用方（CI 的 runner 用户）不是
    # root——下一步的 `cp "$PKGS"/*.apk "$REPO/"` 要覆盖这些文件，
    # 会直接 Permission denied：
    #
    #     cp: cannot create regular file '.../filesystem-1.0-r1.apk': Permission denied
    #
    # ★ 这个坑只在【线上已经有同名包】时才会踩到：
    #   第一次发某个包时 var/repo 里没有它，cp 是新建，root 属主无所谓；
    #   第二次发同一个包（或者线上有、本地也要有）才需要覆盖，那时才炸。
    #   实测就是：发 sed 那次过了，发 base 这次挂在 filesystem/glibc/sed。
    docker run --rm -v "$REPO:/repo" \
        --user "$(id -u):$(id -g)" \
        -e RCLONE_CONFIG_YOJIGEN_TYPE=s3 \
        -e RCLONE_CONFIG_YOJIGEN_PROVIDER=Other \
        -e RCLONE_CONFIG_YOJIGEN_ENDPOINT="$S3_BASE_URL" \
        -e RCLONE_CONFIG_YOJIGEN_ACCESS_KEY_ID="$S3_AK" \
        -e RCLONE_CONFIG_YOJIGEN_SECRET_ACCESS_KEY="$S3_SK" \
        -e RCLONE_CONFIG_YOJIGEN_FORCE_PATH_STYLE=true \
        -e RCLONE_CONFIG_YOJIGEN_REGION=us-east-1 \
        rclone/rclone copy "yojigen:$S3_BUCKET/$ARCH" /repo \
        --include "*.apk" --no-traverse 2>&1 | grep -viE "NOTICE:|^$" | sed 's/^/    /' || true
    SYNCED=1
    echo "    线上一共 $(ls "$REPO"/*.apk 2>/dev/null | wc -l | tr -d ' ') 个包（含已有的）"
else
    echo "  （没有 S3 凭据，跳过拉取线上——本地开发时正常）"
fi

# ★ 用 cp 而不是 mv —— var/pkgs 是"打包的产物"，var/repo 是"要发布的东西"，
#   两者是不同的东西，repo 可以从 pkgs 重新生成。
cp "$PKGS"/*.apk "$REPO/"

ALL=$(ls "$REPO"/*.apk 2>/dev/null | wc -l | tr -d ' ')
[ "$SYNCED" = 1 ] && echo "  建索引时会包含 $ALL 个包（线上的 + 这次的）"

# 签名参数：没给密钥就不签（本地开发够用，装的时候 --allow-untrusted）
if [ -n "$SIGN_KEY" ]; then
    [ -f "$SIGN_KEY" ] || { echo "找不到签名密钥: $SIGN_KEY" >&2; exit 1; }

    # ★ 公钥是【现场从私钥推导】的，不存副本——一个源头，不会漂移。
    #
    # 为什么需要它：mkndx 建索引时会【逐个验证】它索引的包，不信任就拒绝：
    #     ERROR: x.apk: UNTRUSTED signature
    #     ERROR: 1 errors, not creating index
    # 所以 --keys-dir 指的目录里必须有对应的公钥。
    KEYS=$(mktemp -d)
    trap 'rm -rf "$KEYS"' EXIT
    openssl rsa -in "$SIGN_KEY" -pubout -out "$KEYS/$(basename "$SIGN_KEY").pub" 2>/dev/null || {
        echo "从私钥导出公钥失败。是有效的 RSA 私钥吗？" >&2; exit 1
    }

    SIGN_ARGS="--sign-key /key.rsa --keys-dir /keys"
    SIGN_MOUNT="-v $SIGN_KEY:/key.rsa:ro -v $KEYS:/keys:ro"
    echo "  签名: $(basename "$SIGN_KEY")  （公钥现场从它推导）"
else
    SIGN_ARGS="--allow-untrusted"
    SIGN_MOUNT=""
    echo "  ★ 不签名（本地开发用；装的时候要 --allow-untrusted）"
    echo "     签名：scripts/keys.sh gen 然后 $0 --sign-key ~/.yojigenos/keys/yojigenos-2026-1.rsa"
fi

DESC="${DESC:-YojigenOS $ARCH 软件仓库}"

echo
# shellcheck disable=SC2086
docker run --rm -v "$REPO:/repo" $SIGN_MOUNT alpine:latest sh -c "
    set -e
    cd /repo
    rm -f APKINDEX.tar.gz
    apk mkndx $SIGN_ARGS \
        --pkgname-spec 'aarch64/\${name}-\${version}.apk' \
        --description '$DESC' \
        --output APKINDEX.tar.gz *.apk
" 2>&1 | grep -viE '^\s*warning: support for packages without datahash' | sed 's/^/  /' || true

# ★ 检查【产物】而不是退出码。
#   上面那条管道里的 grep 和 sed 会把 mkndx 的退出码吃掉——脚本曾经在
#   "14 errors, not creating index" 之后照样打出一个 ✓。
if [ ! -s "$REPO/APKINDEX.tar.gz" ]; then
    echo >&2
    echo "  ✗ 索引没有生成。上面那些 ERROR 就是原因。" >&2
    echo "    最常见的是包没签名——pack.sh 要带 --sign-key。" >&2
    exit 1
fi

echo
printf '  ✓ var/repo/%s/APKINDEX.tar.gz  %s 字节\n' "$ARCH" "$(wc -c < "$REPO/APKINDEX.tar.gz" | tr -d ' ')"
echo
echo "  本地试一下（起个静态服务，然后从它装）："
echo "      cd var/repo && python3 -m http.server 18080"
echo "      apk add --allow-untrusted --repository http://localhost:18080 \\"
echo "              --root /tmp/r --initdb <包名>"
echo
