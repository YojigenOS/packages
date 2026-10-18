# pip 的「构建」步骤 —— 没有编译，只有安放。
#
# [build] 表的 type = "custom" 让 build() 和 package() 都是空操作，全部
# 由这里接管。

package() {
  # ★★★ 2026-10-18：不再用 `python3 -m ensurepip` —— 改成直接装它带的那个 wheel。
  #
  #   原因和 meson 那次一模一样，而这一处更隐蔽：**`ensurepip` 内部就是起一个
  #   pip，所以它也会说"已经装了"然后什么都不做。**
  #
  #   实测（pip 已经在系统里时装到 --root）：
  #
  #       Looking in links: /tmp/tmpv2ytkelo
  #       Requirement already satisfied: pip in ./usr/lib/python3.14/site-packages
  #       → --root 下 0 个文件
  #
  #   ★ 而 `ensurepip -U` 也【不】够 —— 试过了，同样说 already satisfied。
  #     因为 `--upgrade` 针对的是"已装的那个版本"，而在 `--root` 下它看的是
  #     【宿主】的 site-packages。宿主有 26.2.1，它就认为不必装。
  #
  #   ——而这一条【为什么会突然出现】：同一天我们把 pip 加进了 devel.toml
  #     （构建环境该有它）。**一个正确的改动让另一个地方的条件变了。**
  #
  #   ★ 现在直接用它带的 wheel + `--ignore-installed`（meson 那边已经证明
  #     这个组合管用）：
  #
  #       wheel 的位置是 <stdlib>/ensurepip/_bundled/pip-<版本>-py3-none-any.whl
  #
  #   实测在"系统里已经有 pip 26.2.1"的前提下，它照样铺出 886 个文件、
  #   /usr/bin/pip · pip3 · pip3.14 都在。
  #
  # ★ 而那些脚本的 shebang 是 #!/usr/bin/python3 —— 正是我们系统里的路径，
  #   所以装进去直接能用，不需要改。
  WHEEL=$(echo /usr/lib/python3.*/ensurepip/_bundled/pip-*.whl)
  if [ ! -f "$WHEEL" ]; then
    echo "[产物] ✗ 找不到 ensurepip 带的那个 wheel：${WHEEL}" >&2
    return 1
  fi
  # --no-deps      它没有依赖（ensurepip 带的就是 pip 自己）
  # --no-index     不去 PyPI 找东西 —— 构建过程不联网
  # --ignore-installed  铺到 ${PKGDEST}，不看宿主装了什么（理由见上）
  # --root         装到 $PKGDEST 而不是 /
  python3 -m pip install --no-deps --no-index --ignore-installed \
      --root "$PKGDEST" "$WHEEL"

  # ★ 它不装 setuptools（老版本会）。现代 ensurepip 只带 pip。

  # ensurepip 会打印几行进度，还会在装了 pip 之后提示"用 pip 装东西时
  # 要考虑 root 权限"。那些噪声已经在驱动里被过滤掉了。
}
