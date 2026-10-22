#!/bin/sh
# setuptools —— 装现成的 wheel，没有东西要编。

build() {
  : # 纯 Python wheel，不需要构建
}

package() {
  # 源码是一个 .whl（source.py 解出来的），直接用 pip 装
  W=$(find "$SRCDIR" -maxdepth 2 -name "setuptools-*.whl" | head -1)
  [ -n "$W" ] || { echo "[产物] ✗ 没找到 wheel" >&2; return 1; }
  python3 -m pip install --no-deps --no-index --ignore-installed --root "$PKGDEST" "$W"
}
