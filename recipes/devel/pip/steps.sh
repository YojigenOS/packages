# pip 的「构建」步骤 —— 没有编译，只有安放。
#
# [build] 表的 type = "custom" 让 build() 和 package() 都是空操作，全部
# 由这里接管。

package() {
  # ★ 用 ensurepip，不自己解 wheel。
  #
  #   --root  装到 $PKGDEST 而不是 / （它会自己在下面建 usr/ 那一层）
  #   --default-pip  额外建一个不带版本号的 pip（默认只建 pip3 和 pip3.14）
  #
  # 实测它装出这些东西：
  #
  #     /usr/bin/pip  pip3  pip3.14
  #     /usr/lib/python3.14/site-packages/pip/
  #     /usr/lib/python3.14/site-packages/pip-26.2.1.dist-info/
  #
  # ★ 那些脚本的 shebang 是 #!/usr/bin/python3 —— 正是我们系统里的路径，
  #   所以装进去直接能用，不需要改。
  #
  # ★ 它不装 setuptools（老版本会）。现代 ensurepip 只带 pip。
  python3 -m ensurepip --root "$PKGDEST" --default-pip

  # ensurepip 会打印几行进度，还会在装了 pip 之后提示"用 pip 装东西时
  # 要考虑 root 权限"。那些噪声已经在驱动里被过滤掉了。
}
