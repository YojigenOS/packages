#!/bin/sh
# skirt 的冒烟测试 —— 跑一次真的构建，验证整条链是通的
#
# 用法：
#     ./scripts/smoke.sh              # aarch64（默认）
#     ARCH=x86_64 ./scripts/smoke.sh
#
# 大约 1 分钟。它不替代 CI 的全量自举，而是回答一个更小的问题：
#
#     【现在这条链，此刻，是通的吗？】
#
# ── 为什么要它 ────────────────────────────────────────────────────────────
#
# 2026-10-11 那天修了三个「静默产出错误结果」的驱动 bug：
#
#     run.sh 没有 set -e     → 构建失败变成「成功」，产出一个残缺的包
#     源码在宿主解压         → APFS 大小写不敏感，内核源码 13 组文件只剩 13 个
#     产物目录模式丢了       → /tmp 变成 0755，自举卡在最后一个包 glibc
#
# 三个都不是「跑不通」，而是「跑通了但结果是错的」。而 81 个单元测试
# 一个都没抓到——它们全都不碰 Docker，而三个 bug 都在 Docker 那一侧。
#
# 所以这个脚本做的不是单元测试，是【端到端】：真的下载、真的解压、
# 真的编译、真的运行、真的看输出对不对。
#
# ── 每一项检查对应什么 ────────────────────────────────────────────────────
#
#   ① 解压完整性   对应「源码在宿主解压」那个 bug。不编译，只要几秒。
#   ② 端到端构建   对应「run.sh 没有 set -e」。构建失败必须让这里失败。
#   ③ 产物形态     对应「产出了一个空包也算成功」。
#   ④ 它能跑       对应「产物在容器里存在，但在真实根文件系统里跑不起来」。
#   ⑤ 中文能显示   对应「中文优先」这个承诺——它实现得好好的，但从没被验证过。
#
# ⑤ 用的是 hello 自己的设计意图。它的配方注释里写着：
#
#     「跑 LANG=zh_CN.UTF-8 hello 看到中文，就说明 gettext、NLS、locale
#       三件事都对了——这三件事各自都可能静默失败。」
#
# 那句话写在 2026-10-10，而直到 2026-10-11 才有人真的跑了一次。

set -eu

cd "$(dirname "$0")/.."

ARCH=${ARCH:-$(uname -m)}
[ "$ARCH" = "arm64" ] && ARCH=aarch64
BUILDENV=${SKIRT_BUILDENV:-yojigenos/base-devel}
PROBE=hello

FAILED=""
pass() { printf '  \033[32m✓\033[0m %s\n' "$1"; }
fail() { printf '  \033[31m✗\033[0m %s\n' "$1"; FAILED="$FAILED $2"; }
note() { printf '    %s\n' "$1"; }

echo
echo "  skirt 冒烟测试（${ARCH}）"
echo

# ── ① 解压完整性 ─────────────────────────────────────────────────────────
#
# 内核的 tarball 里有 13 组只差大小写的文件（xt_MARK.h / xt_mark.h 之类）。
# 在宿主上解压就会互相覆盖——而宿主是 macOS，APFS 默认大小写不敏感。
#
# 这条检查不需要编译，所以很快；它只看「解压出来的树里两个文件都在不在」。
# 路径只该有一处：问驱动，不在这里抄一遍。
eval "$(./skirt/skirt paths --shell)"

echo "  ① 源码解压的完整性"
KERNEL_TAR=$(ls "$CACHE_DIR"/linux-*.tar.xz 2>/dev/null | head -1 || true)
if [ -z "$KERNEL_TAR" ]; then
    note "跳过：$CACHE_DIR 里没有内核 tarball（它太大了，不一定留着）"
else
    N=$(docker run --rm -v "$KERNEL_TAR:/src.tar:ro" "$BUILDENV:$ARCH" bash -c '
        mkdir -p /x && tar -xf /src.tar -C /x
        python3 - <<PY
import pathlib, collections
root = next(pathlib.Path("/x").iterdir())
low = collections.defaultdict(list)
for p in root.rglob("*"):
    low[str(p).lower()].append(str(p))
print(sum(1 for v in low.values() if len(v) > 1))
PY
    ' 2>/dev/null || echo 0)
    if [ "$N" = "13" ]; then
        pass "内核源码 13 组只差大小写的文件都在（解压发生在容器里）"
    else
        fail "内核源码只找到 $N 组大小写碰撞，期望 13 —— 解压可能在宿主上做了" "unpack"
    fi
fi

# ── ② 端到端构建 ─────────────────────────────────────────────────────────
#
# 先删掉上一次的产物，否则「构建成功」可能只是在看旧的。
echo "  ② 端到端构建 $PROBE"
rm -rf "$OUTPUT_DIR/$ARCH"/"$PROBE"-*/
if ./skirt/skirt build "$PROBE" --arch "$ARCH" > /tmp/smoke-build.log 2>&1; then
    pass "构建成功"
else
    fail "构建失败——日志在 /tmp/smoke-build.log" "build"
    tail -5 /tmp/smoke-build.log | sed 's|^|      |'
fi
PAYLOAD=$(ls -d "$OUTPUT_DIR/$ARCH"/"$PROBE"-*/payload 2>/dev/null | head -1 || true)

# ── ③ 产物形态 ───────────────────────────────────────────────────────────
echo "  ③ 产物形态"
if [ -z "$PAYLOAD" ]; then
    fail "没有产物目录" "payload"
else
    COUNT=$(find "$PAYLOAD" \( -type f -o -type l \) | wc -l | tr -d ' ')
    if [ "$COUNT" -eq 0 ]; then
        fail "产物是空的——「收集成功」不等于「收到了东西」" "empty"
    else
        pass "产物非空（$COUNT 项）"
    fi
    if [ -x "$PAYLOAD/usr/bin/$PROBE" ]; then
        pass "usr/bin/$PROBE 存在且可执行"
    else
        fail "usr/bin/$PROBE 缺失或不可执行" "binary"
    fi
fi

# ── ④ 它能跑 + ⑤ 中文能显示 ──────────────────────────────────────────────
#
# 两条都在【一个真实的根文件系统里】测，而不是从挂载点直接跑。
# 差别是实的：消息目录的位置是编译进程序的（/usr/share/locale），
# 从别处跑就找不到翻译——那样测出来的「中文不显示」是假阳性。
echo "  ④ 它能跑"
echo "  ⑤ 中文能显示"
LOCALES=$(ls -d "$OUTPUT_DIR/$ARCH"/glibc-locales-*/payload 2>/dev/null | head -1 || true)
if [ -z "$PAYLOAD" ] || [ -z "$LOCALES" ]; then
    note "跳过：缺 hello 或 glibc-locales 的产物"
else
    OUT=$(docker run --rm \
        -v "$PAYLOAD:/in/hello:ro" \
        -v "$LOCALES:/in/loc:ro" \
        "$BUILDENV:$ARCH" bash -c '
        cp -a /in/hello/. / ; cp -a /in/loc/. /
        echo "EN=$(hello)"
        echo "ZH=$(LANG=zh_CN.UTF-8 LC_ALL=zh_CN.UTF-8 hello)"
        echo "TW=$(LANG=zh_TW.UTF-8 LC_ALL=zh_TW.UTF-8 hello)"
    ' 2>/dev/null || echo "")

    EN=$(printf '%s\n' "$OUT" | sed -n 's/^EN=//p')
    ZH=$(printf '%s\n' "$OUT" | sed -n 's/^ZH=//p')
    TW=$(printf '%s\n' "$OUT" | sed -n 's/^TW=//p')

    if [ "$EN" = "Hello, world!" ]; then
        pass "英文输出正确（${EN}）"
    else
        fail "英文输出不对：[$EN]" "run"
    fi

    # 这一条是「中文优先」最小的可执行定义：不是文档里说支持中文，
    # 而是一个真实的程序在这个系统上真的打出了中文。
    if printf '%s' "$ZH" | grep -q '世界你好'; then
        pass "中文输出正确（${ZH}）"
    else
        fail "中文没出来：[$ZH] —— gettext / NLS / locale 三者之一断了" "zh"
    fi

    if printf '%s' "$TW" | grep -q '哈囉'; then
        pass "繁体输出正确（${TW}）"
    else
        fail "繁体没出来：[$TW]" "zh_TW"
    fi
fi

# ── 结果 ─────────────────────────────────────────────────────────────────
echo
if [ -z "$FAILED" ]; then
    printf '  \033[32m全部通过\033[0m —— 这条链此刻是通的\n\n'
else
    printf '  \033[31m有失败\033[0m，失败项:%s\n\n' "$FAILED"
    exit 1
fi
