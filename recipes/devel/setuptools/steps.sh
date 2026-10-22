#!/bin/sh
# setuptools —— 装现成的 wheel，没有东西要编。
#
# ★★ 做法照 meson 那份配方（它是同一个形状的先例）：**用 `$SRCARCHIVE`
#   （原始归档的路径），软链成 `.whl` 再交给 pip**。
#
#   为什么不能"从 $SRCDIR 里找 wheel"：`.whl` 本质是 zip，而驱动**会把 zip
#   解开**（`source.py` 里专门支持 wheel，因为它要读里面的成员名判断单层目录）。
#   所以 `$SRCDIR` 里是一个**解开的**包目录，不是那个 .whl 文件本身。
#   第一次就是这么写错的：`[产物] ✗ 没找到 wheel`。

package() {
  WHEEL="/tmp/$(basename "$SRCARCHIVE" .tar).whl"
  ln -sf "$SRCARCHIVE" "$WHEEL"
  pip install --no-deps --no-index --ignore-installed --root "$PKGDEST" "$WHEEL"

  # ★ 和 meson 那份一样的收尾：pip 会写 direct_url.json（记录"从哪装的"），
  #   那里面有本机路径 —— 不可复现，删掉；INSTALLER 改成 apk。
  DI=$(echo "$PKGDEST"/usr/lib/python3.*/site-packages/setuptools-*.dist-info)
  if [ ! -d "$DI" ]; then
    echo "[产物] ✗ pip 没在 $PKGDEST 下建出 setuptools 的 dist-info —— 它没装东西" >&2
    return 1
  fi
  rm -f "$DI/direct_url.json"
  printf 'apk\n' > "$DI/INSTALLER"
}
