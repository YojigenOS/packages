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

[ -n "$REMOTE" ] || { echo "用法: $0 <rclone-remote>:<桶路径> [--go]" >&2; exit 1; }

SRC="var/repo/$ARCH"
[ -f "$SRC/APKINDEX.tar.gz" ] || { echo "找不到 $SRC/APKINDEX.tar.gz。先跑 scripts/repo.sh。" >&2; exit 1; }

command -v rclone >/dev/null 2>&1 || {
    echo "没有 rclone。装它：" >&2
    echo "    brew install rclone            # macOS" >&2
    echo "    apk add rclone                 # Alpine" >&2
    exit 1
}

COUNT=$(ls "$SRC"/*.apk 2>/dev/null | wc -l | tr -d ' ')
SIZE=$(du -sh "$SRC" | cut -f1)

echo
echo "  源:      $SRC   （$COUNT 个包，$SIZE）"
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

# ── 第一步：传包（除索引外的一切）────────────────────────────────────────
echo "  [1/2] 传包（不含索引）"
rclone copy "$SRC/" "$REMOTE/$ARCH/" \
    --exclude 'APKINDEX.tar.gz' \
    --checksum \
    --progress \
    $DRY

# ── 第二步：传索引（切换点）──────────────────────────────────────────────
echo
echo "  [2/2] 传索引（这一步之后新版本才对外可见）"
rclone copy "$SRC/APKINDEX.tar.gz" "$REMOTE/$ARCH/" \
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
