#!/usr/bin/env bash
#
# 从 var/pkgs/<arch>/*.apk 把 payload 还原回 var/output/<arch>/。
#
# ── 为什么需要它 ────────────────────────────────────────────────────────────
#
# 建一个包要几十分钟，所以有几条路可以不重建就拿回它的产物：
#
#     seed_run      从上一轮 CI 的 artifact 播种
#     跳过          已经建过的（在 var/pkgs 里有 .apk）
#
# **但它们都只拿回了 .apk，没有拿回 payload。**
#
# 于是：一个包【在仓库里】、`skirt build` 却报「依赖 X 还没有构建」——
# 因为 skirt 合并依赖时找的是 var/output/<arch>/<名字>/payload/，而那里是空的。
#
# 实测撞上的：
#
#     ── 建 linux ──
#     [解析] 依赖链: flex → bc → openssl → bzip2 → elfutils → linux
#     skirt: 错误 依赖 openssl 还没有构建
#            （找不到 var/output/x86_64/openssl-3.5.9-1/payload/）
#
# openssl 明明在仓库里、就在 var/pkgs 里躺着——缺的只是那一步还原。
#
# ── 做法 ────────────────────────────────────────────────────────────────────
#
# .apk 是 ADB 格式（apk v3 起），**不是 gzip tar**，`tar -tzf` 解不出来
# （实测：0 条目）。只有 apk 自己解得开：
#
#     apk extract --allow-untrusted <file.apk>
#
# 所以这里起一个容器跑它。容器里跑有两个原因：宿主（CI 是 Ubuntu）没有 apk；
# 而且不能污染宿主。
#
set -euo pipefail

ARCH="${1:-}"
[ -n "$ARCH" ] || { echo "用法: $0 <架构>   （aarch64 / x86_64）" >&2; exit 1; }

PKGS="var/pkgs/$ARCH"
[ -d "$PKGS" ] || { echo "  没有 ${PKGS}，没什么可还原的"; exit 0; }

n=$(ls "$PKGS"/*.apk 2>/dev/null | wc -l | tr -d ' ')
[ "$n" -gt 0 ] || { echo "  $PKGS 里没有 .apk"; exit 0; }

mkdir -p "var/output/$ARCH"

# 版本号从 skirt 自己那儿问，不自己从文件名剥。
#
# ★ 为什么不从文件名推：文件名是 `openssl-3.5.9-r1.apk`，而目录是
#   `openssl-3.5.9-1`——那个 `r` 只出现在文件名里。用 sed 去掉它不是不行，
#   但那是【复制一份 skirt 的命名规则】，两边迟早会漂。
#
# ★ `skirt list` 的第二列是「版本-release」，【不含包名】：
#
#       bc               1.08.2-1     任意精度计算器语言
#
#   所以目录名要拼：<包名>-<第二列>。我第一版直接把第二列当目录名用，
#   结果还原出一堆叫 `1.08.2-1` 的目录——skirt 当然找不到它们。
MAP=$(./skirt/skirt list 2>/dev/null | sed 's/\x1b\[[0-9;]*m//g' \
      | awk 'NF>=2 && $1 !~ /^[╭╰│─]/ {print $1" "$2}')

echo "  要还原 $n 个包（${ARCH}）"

# ★ 容器以 root 跑（apk 要 root），所以写出来的文件也归 root。
#
#   而 skirt 那边以【宿主用户】跑，于是删不掉上一轮的产物：
#
#       skirt: 错误 清理失败: var/output/x86_64/filesystem-1.0-2
#              [Errno 13] Permission denied: '.../payload'
#
#   实测撞上了。所以跑完把属主改回宿主。用宿主自己的 uid/gid，不能写死——
#   CI 上是 runner(1001)，本机是别人。
docker run --rm \
    -v "$PWD/$PKGS:/pkgs:ro" \
    -v "$PWD/var/output/$ARCH:/out" \
    -e "MAP=$MAP" \
    -e "HOST_UID=$(id -u)" \
    -e "HOST_GID=$(id -g)" \
    alpine:latest sh -c '
    set -e
    apk add --no-cache apk-tools >/dev/null 2>&1

    ok=0; skip=0; fail=0
    for f in /pkgs/*.apk; do
        [ -e "$f" ] || continue
        base=$(basename "$f" .apk)

        # 映射里的名字 = 去掉版本和 release 之后的部分。文件名是
        # <名字>-<版本>-r<release>，而名字自己可能带连字符，所以从右往左剥。
        name=$(printf "%s" "$base" | sed -E "s/-[^-]+-r[0-9]+$//")
        ver=$(printf "%s\n" "$MAP" | awk -v n="$name" "\$1==n {print \$2; exit}")

        if [ -n "$ver" ]; then
            # 目录名是 <包名>-<版本>-<release>
            rel="$name-$ver"
        else
            # 子包不走这张表（skirt list 列的是配方），退回文件名推。
            rel=$(printf "%s" "$base" | sed -E "s/-r([0-9]+)$/-\1/")
        fi

        # ★ build_key 的旁路文件 —— 放在【那个 continue 之前】。
        #
        #   两个坑，都是实测撞出来的：
        #
        #   ① 文件名。pack.sh 写的是 `<名字>-<版本>-r<release>.apk.buildkey`
        #      —— `.apk` 在【中间】。而这里我第一版写成 `$base.buildkey`，其中
        #      `base` 是 `basename "$f" .apk`，也就是把 `.apk` 剥掉之后的名字。
        #      于是两边差三个字符，找不到，而症状是"跳过判断说盘上没有 key"
        #      ——**离成因很远**。
        #
        #   ② 位置。"payload 已经在了"的那些包之前会 `continue` 直接走掉，而
        #      那恰恰是最该拿到 key 的一批（它们在 var/output 里已经有目录了）。
        #
        #   所以：用带 .apk 的完整文件名，而且放在 continue 之前。
        if [ -f "/pkgs/$(basename "$f").buildkey" ]; then
            mkdir -p "/out/$rel"
            cp -f "/pkgs/$(basename "$f").buildkey" "/out/$rel/.buildkey"
        fi

        dest="/out/$rel/payload"
        if [ -d "$dest" ]; then skip=$((skip+1)); continue; fi

        mkdir -p "$dest"
        # apk extract 解到当前目录，所以先 cd 进去。
        if (cd "$dest" && apk extract --allow-untrusted "$f" >/dev/null 2>&1); then
            ok=$((ok+1))
        else
            rmdir "$dest" 2>/dev/null || true
            fail=$((fail+1))
            echo "    ✗ 解不开 $(basename "$f")"
        fi
    done
    # 属主还给宿主，否则 skirt 删不掉自己上一轮的产物（见上面那段注释）。
    if [ -n "${HOST_UID:-}" ]; then
        chown -R "$HOST_UID:$HOST_GID" /out 2>/dev/null || true
    fi

    echo "  还原 $ok 个，已有 $skip 个，失败 $fail 个"
    [ "$fail" = 0 ]
'
