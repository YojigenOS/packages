#!/bin/sh
# 把本地仓库部署到云端
#
# 用法：
#     ./scripts/deploy.sh rustfs:yojigenos-repo            # 干跑，只看会传什么
#     ./scripts/deploy.sh rustfs:yojigenos-repo --go       # 真的传
#     ./scripts/deploy.sh rustfs:yojigenos-repo --go --arch aarch64
#
# 目标是一个 rclone remote 加桶路径。S3 兼容的服务都行——RustFS、R2、S3、MinIO。
#
# ── 部署就一件事：把 var/repo/ 的内容弄上去 ─────────────────────────────────
#
#     repo.os.yojigen.cn/            ← 桶的根 = 这个域名
#     └── aarch64/
#         ├── APKINDEX.tar.gz        ← apk 第一个拿的就是它
#         └── …（每个包每个版本一份）
#
#   ★ 客户端写 --repository https://repo.os.yojigen.cn
#     apk 自己拼 https://repo.os.yojigen.cn/aarch64/APKINDEX.tar.gz
#
# ── 两个必须注意的地方 ────────────────────────────────────────────────────
#
# **① 绝不删旧版本。**
#
# 包仓库是【只增不减】的：manifest 钉住版本，半年后还要能按同样的版本重建。
# 所以用 `rclone copy`，绝不能用 `rclone sync`——后者会把目标端多出来的文件
# 删掉，而那正是我们最需要的旧版本。
#
# **② 先传包，后传索引。**
#
# apk 是先拿 APKINDEX、再按索引去拿包的。如果索引先到、而它列的包还没传完，
# 客户端就会 404。反过来则永远是"索引列出的包都已经在"的状态。
#
# 所以分两次：先 copy 除索引外的一切，最后单独 copy 索引。索引是【切换点】。

set -eu

cd "$(dirname "$0")/.."

REMOTE=""
GO=0
ARCH=aarch64

while [ $# -gt 0 ]; do
    case "$1" in
        --go)   GO=1; shift ;;
        --arch) ARCH="$2"; shift 2 ;;
        -h|--help) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        -*) echo "未知参数: $1" >&2; exit 1 ;;
        *) REMOTE="$1"; shift ;;
    esac
done

# ★ rclone 用【容器里的】，本地不用装。
#
# 这台机器是 mac，装 rclone 要 brew；而 CI 上装它又是一条命令。两者版本还可能
# 不同。放进容器里，这件事就只有一个实现——和 tar 那次同一个理由。
#
# 有本机 rclone 就用本机的（快一点），没有就起容器。
if command -v rclone >/dev/null 2>&1; then
    RCLONE="rclone"
else
    # ★ 用【具名 remote】（yojigen:）而不是 :s3: 连接字符串——
    #   只有具名 remote 才读 RCLONE_CONFIG_<名字>_* 环境变量。用 :s3: 的话
    #   它会报 `s3 provider "" not known`，然后拿默认值去连。
    RCLONE="docker run --rm -v $PWD/var/repo:/repo"
    for v in RCLONE_CONFIG_YOJIGEN_TYPE RCLONE_CONFIG_YOJIGEN_PROVIDER \
             RCLONE_CONFIG_YOJIGEN_ENDPOINT RCLONE_CONFIG_YOJIGEN_ACCESS_KEY_ID \
             RCLONE_CONFIG_YOJIGEN_SECRET_ACCESS_KEY RCLONE_CONFIG_YOJIGEN_FORCE_PATH_STYLE \
             RCLONE_CONFIG_YOJIGEN_REGION; do
        RCLONE="$RCLONE -e $v"
    done
    RCLONE="$RCLONE rclone/rclone"
    # 用容器时，源路径是容器里的 /repo
    SRC_IN_CONTAINER=1
fi

# ── 凭据：环境变量优先，省掉交互式 rclone config ────────────────────────────
#
# 本地和 CI 用【同一套】写法。CI 里那几个 organization secret 直接映射过来：
#
#     S3_AK        → RCLONE_CONFIG_R2_ACCESS_KEY_ID
#     S3_SK        → RCLONE_CONFIG_R2_SECRET_ACCESS_KEY
#     S3_BASE_URL  → RCLONE_CONFIG_R2_ENDPOINT
#     S3_BUCKET    → 桶名（不是 remote 配置，是路径的一部分）
#
# 设了这些之后，rclone 就把 `:s3:` 当成一个可用的 remote，不用 rclone config。
if [ -n "${S3_AK:-}" ] && [ -n "${S3_SK:-}" ] && [ -n "${S3_BASE_URL:-}" ]; then
    export RCLONE_CONFIG_YOJIGEN_TYPE=s3
    export RCLONE_CONFIG_YOJIGEN_PROVIDER=Other
    export RCLONE_CONFIG_YOJIGEN_ENDPOINT="$S3_BASE_URL"
    export RCLONE_CONFIG_YOJIGEN_ACCESS_KEY_ID="$S3_AK"
    export RCLONE_CONFIG_YOJIGEN_SECRET_ACCESS_KEY="$S3_SK"
    # ★ RustFS / MinIO 都是路径式。不开这个的话 rclone 会用虚拟主机式
    #   （bucket.host 形式），而那个通常没配 DNS。
    export RCLONE_CONFIG_YOJIGEN_FORCE_PATH_STYLE=true
    # 自建服务没有 region 概念，但 SDK 要一个值
    export RCLONE_CONFIG_YOJIGEN_REGION=us-east-1

    # 没给 remote 的话，用环境变量拼出来的这个
    if [ -z "$REMOTE" ] && [ -n "${S3_BUCKET:-}" ]; then
        REMOTE="yojigen:$S3_BUCKET"
    fi
    echo "  凭据:    来自环境变量（S3_BASE_URL=${S3_BASE_URL}）"
fi

[ -n "$REMOTE" ] || { echo "用法: $0 <rclone-remote>:<桶路径> [--go]" >&2; exit 1; }

SRC="var/repo/$ARCH"
[ -f "$SRC/APKINDEX.tar.gz" ] || { echo "找不到 $SRC/APKINDEX.tar.gz。先跑 scripts/repo.sh。" >&2; exit 1; }



COUNT=$(ls "$SRC"/*.apk 2>/dev/null | wc -l | tr -d ' ')
SIZE=$(du -sh "$SRC" | cut -f1)

echo
echo "  源:      $SRC   （$COUNT 个包，${SIZE}）"
echo "  目标:    $REMOTE/$ARCH/"
echo "  模式:    $([ "$GO" = 1 ] && echo '★ 真的传' || echo '干跑（加 --go 才真的传）')"
echo

# ★ 签名状态：没签名的话客户端每次都要 --allow-untrusted
if [ -f "$SRC/yojigenos.rsa.pub" ]; then
    echo "  签名:    有（客户端不用加 --allow-untrusted）"
else
    echo "  签名:    ★ 没有 —— 客户端装的时候要加 --allow-untrusted"
    echo "           生成密钥后跑 scripts/repo.sh --sign-key <密钥> 再部署"
fi
echo

DRY=""
[ "$GO" = 1 ] || DRY="--dry-run"

# 用容器里的 rclone 时，源路径是容器里的挂载点
[ "${SRC_IN_CONTAINER:-0}" = 1 ] && SRC_FOR_RCLONE="/repo/$ARCH" || SRC_FOR_RCLONE="$SRC/"

# ── 第一步：传包（除索引外的一切）────────────────────────────────────────
echo "  [1/2] 传包（不含索引）"
$RCLONE copy "$SRC_FOR_RCLONE/" "$REMOTE/$ARCH/" \
    --exclude 'APKINDEX.tar.gz' \
    --checksum \
    --progress \
    $DRY

# ── 第二步：传索引（切换点）──────────────────────────────────────────────
echo
echo "  [2/2] 传索引（这一步之后新版本才对外可见）"
$RCLONE copy "$SRC_FOR_RCLONE/APKINDEX.tar.gz" "$REMOTE/$ARCH/" \
    --checksum \
    --progress \
    $DRY

echo
if [ "$GO" = 1 ]; then
    echo "  ✓ 部署完成"
    echo
    echo "  验证（要等反代和桶策略配好）："
    echo "      curl -sSI https://repo.os.yojigen.cn/$ARCH/APKINDEX.tar.gz"
    echo "      apk add --allow-untrusted --repository https://repo.os.yojigen.cn \\"
    echo "              --root /tmp/r --initdb hello"
else
    echo "  这是干跑。加 --go 才真的传。"
fi
echo
echo "  ★ 旧版本【不会被删】——用的是 copy 不是 sync"
echo "     （manifest 钉住版本，半年后还要能按同样的版本重建）"
echo
