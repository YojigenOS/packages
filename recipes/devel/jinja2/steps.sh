#!/bin/sh
# jinja2 —— 纯 Python，pip 装 wheel。
#
# ★ `--no-build-isolation`：不让 pip 为构建另开环境并联网下载 setuptools。
# ★ `--no-deps`：依赖（markupsafe）由配方声明，不在这里拉。

build() {
  python3 -m pip wheel --no-build-isolation --no-deps -w "$BUILDDIR" .
}

package() {
  W=$(echo "$BUILDDIR"/jinja2-*.whl)
  [ -f "$W" ] || { echo "[产物] ✗ 没编出 wheel：$W" >&2; return 1; }
  python3 -m pip install --no-deps --no-index --ignore-installed --root "$PKGDEST" "$W"
}
