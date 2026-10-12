# meson 的安装步骤 —— 装一个 wheel，没有编译。
#
# ── 为什么用 $SRCARCHIVE 而不是 $SRCDIR ──────────────────────────────────
#
# wheel 的安装规则是 pip 的职责：.data/ 里的东西按 sysconfig 铺开、从
# entry_points.txt 生成 /usr/bin/meson、写 RECORD。自己铺就等于重写 pip
# 的一部分——而项目里那条约定是「只借格式和工具，不借包」。
#
# 而 pip 要的是【那个 .whl 文件】，不是解开的一棵树。第一版我写的是
# `pip install "$SRCDIR"/*.whl`，失败了：
#
#     ERROR: Invalid wheel filename (wrong number of parts): '*'
#
# 因为 skirt 解压时把 wheel 的【内容】铺到了 $SRCDIR，那儿没有 .whl 文件。
#
# ── 为什么还要改个名字 ────────────────────────────────────────────────────
#
# 驱动的归档路径叫 /in/src.tar —— 名字是历史遗留，它现在可能是 zip。
# 而 **pip 按扩展名判断**，看到 .tar 就走 tar 分支：
#
#     pip/_internal/utils/unpacking.py: _untar → tarfile.open("/in/src.tar")
#     tarfile.ReadError: file could not be opened successfully
#
# 所以这里先做个符号链接，给它一个正确的扩展名。加 -s 不改内容，也不占空间。

package() {
  WHEEL="/tmp/$(basename "$SRCARCHIVE" .tar).whl"
  ln -sf "$SRCARCHIVE" "$WHEEL"

  # --no-deps    依赖在 spec.toml 里声明，交给 apk 管。
  #              ★ 让 pip 装依赖会绕开 apk，产物里就会出现 apk 不知道的文件。
  # --no-index   不去 PyPI 找东西 —— source 已经下载好了，构建过程不联网。
  # --root       装到 $PKGDEST 而不是 /
  #
  # 它自己会生成 /usr/bin/meson（从 entry_points 里的
  # `meson = mesonbuild.mesonmain:main`），shebang 指向 /usr/bin/python3。
  pip install --no-deps --no-index --root "$PKGDEST" "$WHEEL"

  # pip 会往 dist-info 里写 INSTALLER（内容 "pip"）和 direct_url.json
  # （记着构建机的临时路径）。产物是 apk 装的，前者不对；后者会泄露构建机
  # 的目录结构，而且两者都会让产物漂。
  DI=$(echo "$PKGDEST"/usr/lib/python3.*/site-packages/meson-*.dist-info)
  [ -d "$DI" ] || return 0
  rm -f "$DI/direct_url.json"
  printf 'apk\n' > "$DI/INSTALLER"
}
