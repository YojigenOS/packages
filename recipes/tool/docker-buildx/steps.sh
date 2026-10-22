#!/bin/sh
# docker-buildx —— 把那一个静态二进制放进 CLI 找插件的地方。
#
# 可用的环境变量（由驱动注入）：
#   $SRCDIR      源码目录。unpack = false，所以里面直接就是那份二进制（原文件名）
#   $SRCARCHIVE  原始文件的路径 —— 用它的 basename 拿原文件名（版本变了也不用改脚本）
#   $PKGDEST     安装目标，等于最终根文件系统里的 /

build() {
  : # 没有东西需要编译
}

package() {
  d="$PKGDEST"
  f="$SRCDIR/$(basename "$SRCARCHIVE")"

  # ★ 找不到就失败并列出实际有什么 —— 上游改过资产命名（`buildx-v…` 这个前缀
  #   是 0.x 才有的），而那种改动如果只是"少装一个文件"，症状会是 `docker buildx`
  #   报一句提示装插件的话，完全指不到这里。
  if [ ! -f "$f" ]; then
    echo "✗ 没找到那份二进制: $f" >&2
    echo "  \$SRCDIR 里实际有：" >&2
    ls -1 "$SRCDIR" >&2
    exit 1
  fi

  # ★ 装成 `docker-buildx` 这个名字：CLI 是按 `docker-<子命令>` 找的。
  install -Dm755 "$f" "$d/usr/libexec/docker/cli-plugins/docker-buildx"
}
