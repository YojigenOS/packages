#!/bin/sh
# flit_core —— 装现成的 wheel（做法同 setuptools / meson：走 ${SRCARCHIVE}）。

package() {
  WHEEL="/tmp/$(basename "$SRCARCHIVE" .tar).whl"
  ln -sf "$SRCARCHIVE" "$WHEEL"
  pip install --no-deps --no-index --ignore-installed --root "$PKGDEST" "$WHEEL"

  DI=$(echo "$PKGDEST"/usr/lib/python3.*/site-packages/flit_core-*.dist-info)
  if [ ! -d "$DI" ]; then
    echo "[产物] ✗ pip 没建出 flit_core 的 dist-info" >&2
    return 1
  fi
  rm -f "$DI/direct_url.json"
  printf 'apk\n' > "$DI/INSTALLER"
}
