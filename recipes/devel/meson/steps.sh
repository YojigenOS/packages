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
# 因为 skirt 解压时把 wheel 的【内容】铺到了 ${SRCDIR}，那儿没有 .whl 文件。
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

  # ★★★ `--ignore-installed` 不是可选的。2026-10-18 实测。
  #
  #   背景：构建环境（`yojigenos/devel`）里【已经装了 meson】—— 因为 meson
  #   自己就在 devel.toml 里（它是构建工具）。于是 pip 看到同一个版本，说
  #
  #       meson is already installed with the same version as the provided
  #       wheel. Use --force-reinstall to force an installation of the wheel.
  #
  #   —— 【然后什么都不做】。而 payload 就空了。而包【照样报 ✅】，因为
  #   build() 和这一步的清理都成功了。
  #
  #   ★ 我要的语义不是"管理容器里的安装"，而是"把这个 wheel 铺到 $PKGDEST"。
  #     那正是 `--ignore-installed` 做的事：它不看容器里装了什么，直接铺。
  #     （`--force-reinstall` 也能达到效果，但它会先卸载 —— 在 `--root` 下
  #     卸载是多余的动作，而且它管的是另一个问题。）
  #
  #   ★ 而这一条【为什么会突然出现】：因为同一天我们把 meson 加进了
  #     devel.toml（构建环境该有它，那是另一件事做对了）。**一个正确的改动
  #     让另一个地方的条件变了** —— 而那个地方原来靠的是"环境里没有它"。
  #
  #   ★ 而它是被【新加的 check_dest】挖出来的：那个检查说
  #     「/build/dest 里一个包内容都没有」。在那之前，这一条会一直安静下去，
  #     直到下游的 test_kind 报「devel 但既没有可执行文件，也没有头文件」，  #     而那离成因隔了两小时和一整轮 CI。

  # --no-deps    依赖在 spec.toml 里声明，交给 apk 管。
  #              ★ 让 pip 装依赖会绕开 apk，产物里就会出现 apk 不知道的文件。
  # --no-index   不去 PyPI 找东西 —— source 已经下载好了，构建过程不联网。
  # --root       装到 $PKGDEST 而不是 /
  #
  # 它自己会生成 /usr/bin/meson（从 entry_points 里的
  # `meson = mesonbuild.mesonmain:main`），shebang 指向 /usr/bin/python3。
  pip install --no-deps --no-index --ignore-installed --root "$PKGDEST" "$WHEEL"

  # pip 会往 dist-info 里写 INSTALLER（内容 "pip"）和 direct_url.json
  # （记着构建机的临时路径）。产物是 apk 装的，前者不对；后者会泄露构建机
  # 的目录结构，而且两者都会让产物漂。
  DI=$(echo "$PKGDEST"/usr/lib/python3.*/site-packages/meson-*.dist-info)
  if [ ! -d "$DI" ]; then
    # ★★ 2026-10-18：这里原来是 `|| return 0` —— 它把"pip 什么都没装"也
    #   当成了"正常，不用清理"。而两者完全不是一回事：前者是失败。
    #   现在它会出声（而 check_dest 会更早地拦住它，这里是第二道）。
    echo "[产物] ✗ pip 没在 $PKGDEST 下建出 meson 的 dist-info —— 它没装东西" >&2
    return 1
  fi
  rm -f "$DI/direct_url.json"
  printf 'apk\n' > "$DI/INSTALLER"
}
