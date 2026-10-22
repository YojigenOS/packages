#!/bin/sh
# MarkupSafe —— 用 setuptools 编那个 C 扩展。
#
# ★ `--no-build-isolation`：默认 pip 会为构建单独开一个隔离环境、再从网上
#   **下载** setuptools —— 而构建里不该有网络。关掉它，用我们自己的 python3
#   里那份 setuptools。
#
# ★ `--root`：装到 $PKGDEST 里（和 pip 配方同一个约定）。

build() {
  python3 -m pip wheel --no-build-isolation --no-deps -w "$BUILDDIR" .
}

package() {
  W=$(echo "$BUILDDIR"/markupsafe-*.whl)
  [ -f "$W" ] || { echo "[产物] ✗ 没编出 wheel：$W" >&2; return 1; }
  python3 -m pip install --no-deps --no-index --ignore-installed --root "$PKGDEST" "$W"
}
