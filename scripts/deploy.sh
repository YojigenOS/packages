#!/bin/bash
# 把本地仓库部署到云端
#
# 用法：
#     ./scripts/deploy.sh rustfs:yojigen-os-repo            # 干跑，只看会传什么
#     ./scripts/deploy.sh rustfs:yojigen-os-repo --go       # 真的传
#     ./scripts/deploy.sh rustfs:yojigen-os-repo --go --arch aarch64
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
# **① 这个脚本只做"加"，不做"减"。**
#
# 所以用 `rclone copy`，绝不能用 `rclone sync`——后者会把目标端多出来的文件
# 删掉，而那是另一个动作、该由另一个地方决定。
#
# ★ 这一段原来写的是「**绝不删旧版本**」，理由写的是「manifest 钉住版本，
#   半年后还要能按同样的版本重建」。**那个理由 2026-10-15 被推翻了** ——
#   manifest 是【记录】不是【承诺】：它记录这台机器当时从哪些包建出来，而
#   不承诺将来还能把你装的那一组具体 `-rX` 重新拼出来。
#
#   真正要能重建的是【某个提交上的配方】，那件事由 build_key 那套保证。
#   而清理仓库的三种范围、以及"先删后重建索引"的顺序，见决策记录
#   `0020-软件包的清理策略.md`。清理由 `skirt prune` 做（默认干跑）。
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
[ -f "$SRC/APKINDEX.tar.gz" ] || { echo "找不到 $SRC/APKINDEX.tar.gz。先跑 ./skirt/skirt repo。" >&2; exit 1; }



COUNT=$(ls "$SRC"/*.apk 2>/dev/null | wc -l | tr -d ' ')
SIZE=$(du -sh "$SRC" | cut -f1)

echo
echo "  源:      $SRC   （$COUNT 个包，${SIZE}）"
echo "  目标:    $REMOTE/$ARCH/"
echo "  模式:    $([ "$GO" = 1 ] && echo '★ 真的传' || echo '干跑（加 --go 才真的传）')"
echo

# ★ 签名状态：没签名的话客户端每次都要 --allow-untrusted
#
# ★ 这里原来查的是 "$SRC/yojigenos.rsa.pub"——那个文件【从来不存在】。
#   签名是打在 APKINDEX.tar.gz【里面】的一个成员
#   （.SIGN.RSA.<密钥名>.rsa.pub），不是仓库目录里的独立文件。所以这个检查
#   永远走 else 分支，报「没有签名」，而实际是签了的。
#
#   ★ 也【不能】靠读索引自己判断：
#     · 索引是 ADB 格式，不是 gzip tar —— `tar -tzf` 什么都解不出来
#     · 里面的签名标记没有可 grep 的原始字节（`# sig v00 …` 是 apk adbdump
#       的【渲染】，不是文件里的字符串），只有 apk 自己说得准，而那要起容器
#
#   所以改成让【签名的那一方】留记号：`skirt repo` 签完写 var/.repo-signed-<arch>
#   （写在 skirtlib/repo.py 的 mark_signed 里）。
#   与其在部署这边重新推导一遍"签没签"，不如让知道的那一方记下来。
if [ -f "var/.repo-signed-$ARCH" ]; then
    echo "  签名:    有（客户端不用加 --allow-untrusted）"
    echo "           密钥: $(cat "var/.repo-signed-$ARCH")"
else
    echo "  签名:    ★ 没有 —— 客户端装的时候要加 --allow-untrusted"
    echo "           生成密钥后跑 ./skirt/skirt repo --sign-key <密钥> 再部署"
fi
echo

DRY=""
[ "$GO" = 1 ] || DRY="--dry-run"

# 用容器里的 rclone 时，源路径是容器里的挂载点
[ "${SRC_IN_CONTAINER:-0}" = 1 ] && SRC_FOR_RCLONE="/repo/$ARCH" || SRC_FOR_RCLONE="$SRC/"

# ── 第一步：传包（除索引外的一切）────────────────────────────────────────
echo "  [1/2] 传包（不含索引）"
#
# ★★ 强制分片上传。
#
#   rclone 的 --s3-upload-cutoff 默认是 200Mi——**小于它的文件一次 PUT 传完**。
#   而前面那层 nginx 对【单个请求】的 body 有上限，于是大的包被挡：
#
#       ERROR : gcc-16.2.0-r1.apk:     StatusCode: 413   (95.3 MiB)
#       ERROR : python3-3.14.8-r1.apk: StatusCode: 413   (61.6 MiB)
#       47 / 49 传完了，就这两个大的被挡
#
#   （413 = Payload Too Large。后面还跟着一句「XML syntax error: element <hr>
#     closed by </body>」——那个"XML"其实是 nginx 的 HTML 错误页。）
#
#   S3 的分片上传（multipart）把大文件拆成多个 HTTP 请求，每个分片是独立的
#   PUT。所以只要把 cutoff 调到比那两个文件小，限制就绕过去了——**不用改
#   服务端**。
#
#   8Mi 这个值：让 8Mi 以上的文件都走分片，而每片是下面的 chunk-size（5Mi），
#   远低于任何合理的请求体上限。小文件仍然一次传完（少几次往返）。
#
#   ★ 为什么不是"把包做小"：gcc 95MiB 是编译器本体，已经 strip 过了。为了绕
#     一个上传限制去拆包是本末倒置——而且拆完还是要传同样多的字节。
#
#   ★ aarch64 从来没撞上，因为它的仓库里没有 gcc。这是发布一个 95MiB 的包
#     才暴露出来的。
$RCLONE copy "$SRC_FOR_RCLONE/" "$REMOTE/$ARCH/" \
    --exclude 'APKINDEX.tar.gz' \
    --checksum \
    --s3-upload-cutoff 8Mi \
    --s3-chunk-size 5Mi \
    --s3-upload-concurrency 4 \
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
