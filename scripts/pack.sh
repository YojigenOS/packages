#!/bin/bash
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
ALL=0
SIGN_KEY="${SIGN_KEY:-}"
TARGETS=""
while [ $# -gt 0 ]; do
    case "$1" in
        --arch) ARCH="$2"; shift 2 ;;
        --sign-key) SIGN_KEY="$2"; shift 2 ;;
        --all)  ALL=1; shift ;;
        -h|--help) sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        -*) echo "未知参数: $1" >&2; exit 1 ;;
        *) TARGETS="$TARGETS $1"; shift ;;
    esac
done
[ -n "$TARGETS" ] || [ "$ALL" = 1 ] || { echo "用法: $0 <包名>… 或 --all" >&2; exit 1; }
if [ -n "$SIGN_KEY" ]; then
    [ -f "$SIGN_KEY" ] || { echo "找不到签名密钥: $SIGN_KEY" >&2; exit 1; }
    echo "  签名: $(basename "$SIGN_KEY")"
fi

OUT="$PWD/var/pkgs/$ARCH"
mkdir -p "$OUT"
WORK=$(mktemp -d)
mkdir -p "$WORK/pack"   # inner.sh 放这里面——挂目录比挂单文件可靠
trap 'rm -rf "$WORK"' EXIT

# 找产物目录。名字后面必须跟 -数字，否则 hello 会匹配到 hello-world。
if [ "$ALL" = 1 ]; then
    DIRS=$(ls -d var/output/"$ARCH"/*/ 2>/dev/null || true)
else
    DIRS=""
    for want in $TARGETS; do
        one=$(ls -d var/output/"$ARCH"/"$want"-[0-9]*/ 2>/dev/null | sort -V | tail -1 || true)
        [ -n "$one" ] && DIRS="$DIRS $one"
    done
fi

[ -n "${DIRS// /}" ] || { echo "找不到产物。先 skirt build 建它。" >&2; exit 1; }

N_OK=0; N_FAIL=0; N_SKIP=0

for d in $DIRS; do
    d="${d%/}"
    [ -f "$d/meta.json" ] || { echo "  跳过 $(basename "$d")（没有 meta.json）"; N_SKIP=$((N_SKIP+1)); continue; }

    # ★ 每个包一个【唯一的】pack 目录。
    #
    # 原来是所有包共用一个 $WORK/pack，里面的 inner.sh 和 epoch 每个包重写
    # 一次、挂载点却始终是同一个宿主路径。实测在 macOS 上会读到【上一个包】
    # 的内容：
    #
    #     /pack/inner.sh: line 13: syntax error: unterminated quoted string
    #
    # 而那个文件在宿主上是对的（字段数不同的包写到第 13 行就断了）。所以是
    # bind mount 对同一路径的快速重写没跟上。
    #
    # 独立的症状：失败的是【随机的包】——每跑一次换一批，而单独打某个包
    # 总是成功。因为单独跑时那个路径上没有"上一次"。
    #
    # ★ 用包名而不是 tmpdir：出问题时路径里能看出是哪个包。
    PACKDIR="$WORK/pack/$(basename "$d")"
    mkdir -p "$PACKDIR"

    # meta.json → 内层脚本。生成脚本而不是拼命令行，是为了绕开引号：
    # 描述里有中文、空格、逗号，直接塞进 sh -c 会被吃掉。
    # 第三个参数是【容器里】的输出目录（挂载点是 /out），不是宿主路径。
    python3 - "$d" "$PACKDIR/inner.sh" /out "${SIGN_KEY:+yes}" <<'PY'
import json, pathlib, shlex, sys
d, out_script, out_dir = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]), sys.argv[3]
signed = len(sys.argv) > 4 and sys.argv[4] == "yes"
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

# ── ★ 扫 ELF，把动态库依赖补上 ────────────────────────────────────────────
#
# mkpkg 不扫描 ELF（实测它建出来的包 provides 和 depends 都是空的；Alpine 的
# so: 是 abuild + scanelf 生成的）。所以在这之前，动态库依赖一直靠人在
# spec.toml 里手写——而手写就会漏。已经漏过的：
#
#     gawk       NEED libmpfr.so.6 + libgmp.so.10，只声明了 glibc
#     apk-tools  NEED libssl.so.3（通过 libapk），得靠人想起来写 openssl
#
# docs/记录/未声明依赖的审计.md 是【离线的一次性分析】：发现了就手改进配方，
# 下次可能又漂。这里把同一件事接进打包流程。
#
# ★ 两端都要：depends 里的 so: 靠 provides 里的 so: 解析。只做一端的话，
#   装了包会发现"没有谁提供 libz.so.1"。
import importlib.util as _ilu
_spec = _ilu.spec_from_file_location("elfdeps", "scripts/elf-deps.py")
_elfdeps = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_elfdeps)

_so_needed, _so_provided = _elfdeps.scan(d / "payload")
# 去重：手写的是包名（glibc、openssl），自动的是 soname（so:libc.so.6）。
# 两者可以同时存在——apk 会两个都满足，多一个约束不改变结果。
_so_deps = [f"so:{n}" for n in _so_needed]
_depends = list(m.get("depends") or []) + [x for x in _so_deps if x not in (m.get("depends") or [])]
_so_provides = [f"so:{k}={v}" if v else f"so:{k}" for k, v in sorted(_so_provided.items())]
_provides = list(m.get("provides") or []) + [x for x in _so_provides if x not in (m.get("provides") or [])]

_extra = len(_so_deps) + len(_so_provides)
if _extra:
    print(f"  [ELF] {name}: +{len(_so_deps)} 个 so: 依赖 ✗ +{len(_so_provides)} 个 so: 提供", file=sys.stderr)

fields = [
    ("name",        name),
    ("version",     version),
    ("description", m.get("description", "")),
    ("url",         m.get("url", "")),
    ("license",     m.get("license", "")),
    ("arch",        m.get("arch", "")),
    ("origin",      m.get("built_from", "")),
    ("depends",     " ".join(_depends)),
]

# provides / provider-priority：这个包提供什么虚名（比如 /bin/sh），以及
# 上面扫出来的 so: 名字。
# 空值不要传——mkpkg 对空字符串会报错，所以下面统一过滤。
if _provides:
    fields.append(("provides", " ".join(_provides)))
if m.get("provider_priority") is not None:
    fields.append(("provider-priority", str(m["provider_priority"])))

# ★ 签名必须在这里做——包建出来就是签好的。
#   不然后面 mkndx 建索引时会逐个验证，全部报 UNTRUSTED signature，
#   然后拒绝建索引（"14 errors, not creating index"）。
sign_arg = "--sign-key /key.rsa " if signed else ""

lines = ["#!/bin/sh", "set -e",
         # ★ getcap/setcap 来自 libcap，而 alpine:latest 里没有它。
         #   装一次（从 Alpine 自己的仓库，很快）比"能力位静默地丢掉"划算。
         #   装不上就继续 —— 下面的 getcap 会失败，而那时 .caps 是空的，
         #   等于退回"没有能力位"的旧行为，不会把构建弄挂。
         "apk add --no-cache libcap >/dev/null 2>&1 || true",
         "mkdir -p /data", "cp -a /in/payload/. /data/",
         # ★★ 属主统一成 root:0，而 setuid/setgid 位要保住。
         #
         #   为什么需要这一步：payload 是从容器里【拷到宿主】再挂回来的，而
         #   沿途有一次清理用的 `chown -R "$HOSTUID"`（见 build.py 的 cleanup）。
         #   于是包里的属主成了构建用户（1001），而不是 root。
         #
         #   实测的两头：
         #       dest 里（package 之后，诊断打的）  -rwsr-xr-x root  root  sudo ✓
         #       发出去的包里（解开发出来的）      -rwxr-xr-x nobody nobody ✗
         #
         #   而"属主不是 root"这件事的后果我们撞过两次：
         #       · sudo      没有 setuid 就不叫 sudo
         #       · .ssh      StrictModes 拒绝非 root 拥有的 authorized_keys
         #                   → 在运行中的系统上升级 openssh 之后 SSH 直接进不去
         #
         #   所以【发行版的包，文件就该是 root:0】。这是个决定，不是默认值 ——
         #   而上游的 Makefile 本来也是这么装的（install -o 0 -g 0）。
         #
         #   ★ 顺序：先记下带 setuid/setgid 的那些（连同它们的完整权限），
         #     再 chown，再把权限放回去 —— 因为 Linux 的 chown 会清掉那两位。
         "find /data \\( -perm -4000 -o -perm -2000 \\) -type f 2>/dev/null"
         " | while IFS= read -r f; do echo \"$(stat -c %a \"$f\") $f\"; done > /tmp/.setid || true",
         # ★ 能力位也要记 —— 它的道理和 setuid 完全一样：chown 会清掉
         #   security.capability。（iputils 给 ping 设的 cap_net_raw 就是这么丢的。）
         #   而 getcap 在 alpine:latest 里没有，所以下面那行 apk add。
         "getcap -r /data 2>/dev/null > /tmp/.caps || true",
         "chown -R 0:0 /data",
         "while IFS=' ' read -r m f; do chmod \"$m\" \"$f\" 2>/dev/null || true; done < /tmp/.setid || true",
         "while read -r f c; do [ -n \"$c\" ] && setcap \"$c\" \"$f\" 2>/dev/null || true; done < /tmp/.caps",
         f"exec apk mkpkg {sign_arg}\\"]
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
    SIGN_MOUNT=""
    [ -n "$SIGN_KEY" ] && SIGN_MOUNT="-v $SIGN_KEY:/key.rsa:ro"
    # shellcheck disable=SC2086
    docker run --rm -e "SOURCE_DATE_EPOCH=$(cat "$PACKDIR/epoch" 2>/dev/null || echo 0)" \
         -v "$PWD/$d:/in:ro" -v "$OUT:/out" -v "$PACKDIR:/pack:ro" $SIGN_MOUNT \
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
            echo "      退出码: $?   挂载: $d → /in，$PACKDIR → /pack，$OUT → /out"
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
