#!/bin/sh
# bash 的构建步骤 —— 只覆盖 package()，为了多建一个 /bin/sh 符号链接。
#
# ★ 为什么要有这一步：`bash-binsh` 那个子包的内容就是 `usr/bin/sh` 这一个
#   符号链接（见 spec.toml 里那段说明）。而 autotools 的默认 package() 只做
#   `make install`，它不会建 /bin/sh。
#
# ★★ 用【相对】链接 `bash` 而不是绝对路径：usr-merge 之后 /bin 是 → usr/bin，
#    所以 usr/bin/sh 里写 `bash` 就指向同一个目录里的 bash。

package() {
  make DESTDIR="$PKGDEST" install

  # 子包收的是这个路径（spec.toml 的 files = ["usr/bin/sh"]）
  ln -sf bash "$PKGDEST/usr/bin/sh"
}
