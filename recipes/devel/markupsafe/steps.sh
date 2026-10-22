#!/bin/sh
# markupsafe —— 用 pip 从 sdist 编出 wheel，再装进 $PKGDEST。
#
# ★★ 两个坑（2026-10-22 都撞过）：
#
#   1. `$BUILDDIR` 只在 `out_of_tree = true` 时才有 —— custom 类型不给。
#      所以自己在 `$SRCDIR` 下开一个确定的目录。
#
#   2. **`build` 和 `package` 是【分两次调用】run.sh 的**（package 要以 root 跑）。
#      所以 build 阶段产出的东西必须落在**两个阶段都看得见**的地方 ——
#      `/tmp/wheels`（同一个容器里）。不要用 $BUILDDIR（它只在一次调用里有意义）。
#
# ★ `--no-build-isolation`：默认 pip 会为构建另开隔离环境、**从网上下载**
#   setuptools —— 构建里不该有网络。关掉它，用我们自己那份（devel/setuptools）。
# ★ `--no-deps`：依赖由配方声明，不在 pip 那层再拉一遍（那会绕过我们的仓库）。

WHLDIR=/tmp/wheels

build() {
  rm -rf "$WHLDIR"; mkdir -p "$WHLDIR"
  python3 -m pip wheel --no-build-isolation --no-deps -w "$WHLDIR" .
}

package() {
  W=$(echo "$WHLDIR"/*.whl)
  [ -f "$W" ] || { echo "[产物] ✗ $WHLDIR 里没有 wheel" >&2; ls -la "$WHLDIR" >&2; return 1; }
  pip install --no-deps --no-index --ignore-installed --root "$PKGDEST" "$W"

  # 和 meson / setuptools 那份一样的收尾：direct_url.json 里有本机路径（不可复现）
  DI=$(echo "$PKGDEST"/usr/lib/python3.*/site-packages/markupsafe-*.dist-info)
  [ -d "$DI" ] && rm -f "$DI/direct_url.json" && printf 'apk\n' > "$DI/INSTALLER"
  return 0
}
