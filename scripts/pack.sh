#!/bin/sh
# 把一个已建好的包打成 .apk
#
# 用法：
#     ./scripts/pack.sh hello                  # 按名字找最新的产物
#     ./scripts/pack.sh hello glibc bash       # 打几个
#     ./scripts/pack.sh --all                  # 打全部
#     ./scripts/pack.sh --all --arch x86_64
#
# ── 建包和打包是两件事 ────────────────────────────────────────────────────
#
#     skirt build <包>    配方 → var/output/<arch>/<包>-<版本>-<发布号>/
#                                   ├── payload/     文件树
#                                   └── meta.json    身份 + 依赖 + 日期
#     pack.sh                  → var/pkgs/<arch>/<包>-<版本>-r<发布号>.apk
#
# ── 为什么不用 skirt ──────────────────────────────────────────────────────
#
# 因为【没有东西需要自己实现】。apk-tools 3.0 自带全套：
#
#     apk mkpkg    创建包（v3 格式：魔数是 "ADB"，不是 tar.gz）
#     apk adbsign  签名 / 重签
#     apk mkndx    创建仓库索引
#     apk manifest 读包的校验和
#     apk extract  解包
#
# 这个脚本唯一有技术含量的事是【meta.json → mkpkg 的参数】。其余的字节
# 我们一行都不碰——v3 格式长什么样不需要知道，因为生成用 mkpkg、读用 apk。
#
# ── 为什么用 alpine:latest 而不是 base-devel ──────────────────────────────
#
# 打包不需要编译器。而 base-devel 里也没有 apk-tools——它是我们自举出来的
# glibc 系统，apk-tools 要等 5.8 那条链（perl → openssl → curl → apk-tools）。
#
# apk-tools 在这里是【构建期的工具】，不进交付物。用 Alpine 的现成版本
# 正是 0006 说的「只借格式和工具，不借包」。
#
# ── mkpkg 的坑（都是试出来的）────────────────────────────────────────────
#
#   · --info 用【冒号】分隔：--info name:hello
#     写成 name=hello 报 "missing key or value"，写成两个参数也一样
#   · 必填只有 name 和 version；version 要含发布号（2.12.3-r1）
#   · 空值不要传——mkpkg 对空字符串会报错

set -eu

cd "$(dirname "$0")/.."

ARCH=aarch64
TARGETS=""
while [ $# -gt 0 ]; do
    case "$1" in
        --arch) ARCH="$2"; shift 2 ;;
        --all)  TARGETS="$TARGETS --all"; shift ;;
        -h|--help) sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        -*) echo "未知参数: $1" >&2; exit 1 ;;
        *) TARGETS="$TARGETS $1"; shift ;;
    esac
done
[ -n "$TARGETS" ] || { echo "用法: $0 <包名>… 或 --all" >&2; exit 1; }

OUT="$PWD/var/pkgs/$ARCH"
mkdir -p "$OUT"
WORK=$(mktemp -d)
mkdir -p "$WORK/pack"   # inner.sh 放这里面——挂目录比挂单文件可靠
trap 'rm -rf "$WORK"' EXIT

# 找产物目录。名字后面必须跟 -数字，否则 hello 会匹配到 hello-world。
if [ "$TARGETS" = "--all" ]; then
    DIRS=$(ls -d var/output/"$ARCH"/*/ 2>/dev/null || true)
else
    DIRS=""
    for want in $TARGETS; do
        [ "$want" = "--all" ] && continue
        one=$(ls -d var/output/"$ARCH"/"$want"-[0-9]*/ 2>/dev/null | sort -V | tail -1 || true)
        [ -n "$one" ] && DIRS="$DIRS $one"
    done
fi

[ -n "${DIRS// /}" ] || { echo "找不到产物。先 skirt build 建它。" >&2; exit 1; }

N_OK=0; N_FAIL=0; N_SKIP=0

for d in $DIRS; do
    d="${d%/}"
    [ -f "$d/meta.json" ] || { echo "  跳过 $(basename "$d")（没有 meta.json）"; N_SKIP=$((N_SKIP+1)); continue; }

    # meta.json → 内层脚本。生成脚本而不是拼命令行，是为了绕开引号：
    # 描述里有中文、空格、逗号，直接塞进 sh -c 会被吃掉。
    # 第三个参数是【容器里】的输出目录（挂载点是 /out），不是宿主路径。
    python3 - "$d" "$WORK/pack/inner.sh" /out <<'PY'
import json, pathlib, shlex, sys
d, out_script, out_dir = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]), sys.argv[3]
m = json.loads((d / "meta.json").read_text())

name    = m["name"]
version = f"{m['version']}-r{m['release']}"
apk     = f"{name}-{version}.apk"

# ★ 时间戳不通过 --info 传 —— mkpkg 不认 builddate 这个字段
#   （ERROR: invalid info field: builddate）。
#
# 它认的是【环境变量 SOURCE_DATE_EPOCH】——正是 0005 从第一天就在用的那个
# 通用约定。实测：同一个包换一个时间戳，产出字节就不同；不设的话它用当前
# 时间，于是同样的输入会产出不同的包（xz 连打三次得到 620941/620941/620943）。
#
# 时间戳写进一个文件，外层脚本拿它设环境变量。用 meta.json 的 built_at，
# 它已经是确定性的：max(配方声明的日期, 依赖的日期)。
import datetime
def _epoch(iso: str) -> str:
    try:
        return str(int(datetime.datetime.fromisoformat(iso).timestamp()))
    except Exception:
        return "0"

(pathlib.Path(out_script).parent / "epoch").write_text(_epoch(m.get("built_at", "")))

fields = [
    ("name",        name),
    ("version",     version),
    ("description", m.get("description", "")),
    ("url",         m.get("url", "")),
    ("license",     m.get("license", "")),
    ("arch",        m.get("arch", "")),
    ("origin",      m.get("built_from", "")),
    ("depends",     " ".join(m.get("depends") or [])),
]

lines = ["#!/bin/sh", "set -e", "mkdir -p /data", "cp -a /in/payload/. /data/",
         "exec apk mkpkg \\"]
for k, v in fields:
    if v:
        lines.append(f"  --info {shlex.quote(f'{k}:{v}')} \\")
lines.append(f"  --files /data --output {shlex.quote(out_dir + '/' + apk)}")
out_script.write_text("\n".join(lines) + "\n")
out_script.chmod(0o755)
print(apk)
PY
    APK=$(python3 -c "
import json,pathlib,sys
m=json.loads((pathlib.Path('$d')/'meta.json').read_text())
print(f\"{m['name']}-{m['version']}-r{m['release']}.apk\")")

    printf '  %-30s ' "$APK"
    rm -f "$OUT/$APK"
    docker run --rm -e "SOURCE_DATE_EPOCH=$(cat "$WORK/pack/epoch" 2>/dev/null || echo 0)" \
         -v "$PWD/$d:/in:ro" -v "$OUT:/out" -v "$WORK/pack:/pack:ro" \
         alpine:latest sh /pack/inner.sh > "$WORK/log" 2>&1 || true
    # ★ 两道检查，缺一不可：
    #   退出码是 0 不等于【产出了东西】。上面那次就是这样——docker 成功了，
    #   文件根本没生成，而脚本报了 ✓。让错误传上来还不够，还要检查结果。
    if [ -s "$OUT/$APK" ]; then
        printf '✓ %s 字节\n' "$(wc -c < "$OUT/$APK" | tr -d ' ')"
        N_OK=$((N_OK+1))
    else
        printf '✗\n'
        # 日志可能是空的。空日志本身是信息——说明 docker 都没跑起来，
        # 那种情况要看退出码和挂载，而不是看 mkpkg 的报错。
        if [ -s "$WORK/log" ]; then
            sed 's/^/      /' "$WORK/log" | head -8
        else
            echo "      （docker 没有任何输出——它可能没跑起来）"
            echo "      退出码: $?   挂载: $d → /in，$WORK/pack → /pack，$OUT → /out"
        fi
        N_FAIL=$((N_FAIL+1))
    fi
done

echo
printf '  打了 %s 个' "$N_OK"
[ "$N_FAIL" -gt 0 ] && printf '，失败 %s 个' "$N_FAIL"
[ "$N_SKIP" -gt 0 ] && printf '，跳过 %s 个' "$N_SKIP"
printf '\n  输出: %s\n\n' "${OUT#$PWD/}"
[ "$N_FAIL" -eq 0 ]
