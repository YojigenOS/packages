#!/bin/sh
# 在 QEMU 里启动 YojigenOS
#
# 用法：
#     scripts/run-vm.sh                     # aarch64，Apple Silicon 上是硬件加速的
#     ARCH=x86_64 scripts/run-vm.sh         # x86_64（模拟，慢很多）
#     MEM=2G scripts/run-vm.sh              # 给多点内存
#
# 它做四件事：
#
#     1. 用 skirt 把 rootfs 拼出来（包列表取自 variants/base.toml，加一个 /init）
#     2. 把 rootfs 打成 initramfs（cpio.gz）
#     3. 找回内核镜像
#     4. 一条 QEMU 命令启动
#
# 【为什么用 initramfs 而不是磁盘镜像】
#
# 内核把 initramfs 解开到内存里的 rootfs，然后 exec /init——不需要分区表、
# 不需要文件系统驱动、不需要引导器。这是「能开机」最短的一条路，而磁盘镜像
# 和 ISO 是另一种交付形式（决策记录 0007 把它们分开），需求不一样，以后再说。
#
# 【为什么 cpio 那步要进容器】
#
# 做 initramfs 要用 GNU cpio 的 newc 格式。macOS 自带的是 BSD cpio，参数和
# 行为都不一样。既然这台机器上一定有 Docker（skirt 就靠它），那就用它做——
# 比在宿主机上装一套 GNU 工具可靠。

set -eu

cd "$(dirname "$0")/.."

ARCH=${ARCH:-$(uname -m)}
[ "$ARCH" = "arm64" ] && ARCH=aarch64
MEM=${MEM:-1G}
FLAVOR=${FLAVOR:-base}

case "$ARCH" in
    aarch64) QEMU=qemu-system-aarch64; MACHINE=virt;    CONSOLE=ttyAMA0; CPU=host ;;
    x86_64)  QEMU=qemu-system-x86_64;  MACHINE=q35;     CONSOLE=ttyS0;   CPU=host ;;
    *) echo "不认识的架构: $ARCH" >&2; exit 1 ;;
esac

RUN=output/$ARCH/.vm
mkdir -p "$RUN"

# ---------------------------------------------------------------- 1. 包列表
#
# 从 variants/$FLAVOR.toml 读，而不是在这里再抄一份——两张列表一定会漂移，
# 而漂移的表现是「VM 里的系统比镜像里少一个包」这种很难查的现象。
echo "[1/4] 读风味 $FLAVOR 的包列表"
PACKAGES=$(python3 - "$FLAVOR" <<'PY'
import sys, tomllib, pathlib
v = tomllib.loads(pathlib.Path(f"variants/{sys.argv[1]}.toml").read_text())
print(" ".join(v["variant"]["packages"]))
PY
)

# /init 是启动的必需品，但它不属于任何一个「用户态风味」——容器版不需要它。
# 所以在这里加，而不是塞进 base.toml。
PACKAGES="$PACKAGES yojigenos-init"

# ---------------------------------------------------------------- 2. rootfs
echo "[2/4] 拼 rootfs"
ROOTFS="$RUN/rootfs.tar.gz"
./tools/skirt rootfs --arch "$ARCH" --output "$ROOTFS" $PACKAGES

# ---------------------------------------------------------------- 3. initramfs
echo "[3/4] 打成 initramfs"
INITRAMFS="$RUN/initramfs.cpio.gz"
docker run --rm \
    -v "$PWD/$ROOTFS:/rootfs.tar.gz:ro" \
    -v "$PWD/$RUN:/out" \
    yojigenos/base-devel:"$ARCH" bash -c '
        set -e
        mkdir -p /root && tar -xzf /rootfs.tar.gz -C /root
        # 内核要的核心前提：/init 存在且可执行。找不到时给一句人话，
        # 而不是等到 QEMU 里报 "Unable to open initial console"。
        if [ ! -x /root/init ]; then
            echo "  ✗ rootfs 里没有可执行的 /init —— yojigenos-init 没进包列表？" >&2
            exit 1
        fi
        cd /root
        find . -print0 | cpio --null -o -H newc --quiet | gzip -9 > /out/initramfs.cpio.gz
        echo "    initramfs: $(du -h /out/initramfs.cpio.gz | cut -f1)"
    '

# ---------------------------------------------------------------- 4. 内核
echo "[4/4] 找内核"
KERNEL=$(ls -d output/"$ARCH"/linux-*/payload/boot/vmlinuz 2>/dev/null | head -1 || true)
if [ -z "$KERNEL" ]; then
    echo "  ✗ 没找到内核镜像。" >&2
    echo "    先建它：./tools/skirt build linux --arch $ARCH" >&2
    exit 1
fi
echo "    内核: $KERNEL"

# ---------------------------------------------------------------- 启动
echo
echo "  启动 YojigenOS（$ARCH）—— 退出 QEMU 按 Ctrl-A 然后 X"
echo
exec "$QEMU" \
    -machine "$MACHINE" \
    -cpu "$CPU" \
    -accel hvf \
    -m "$MEM" \
    -kernel "$KERNEL" \
    -initrd "$INITRAMFS" \
    -append "console=$CONSOLE rdinit=/init" \
    -nographic
