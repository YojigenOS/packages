# ca-certificates 的构建步骤 —— 把 certdata.txt 变成 PEM bundle。
#
# 可用的环境变量（由驱动注入）：
#   $PKGDEST    安装目标，等于最终根文件系统里的 /
#   $PKGDIR     本配方目录的副本，make-ca-bundle.py 在它下面
#   $SRCDIR     源码目录，certdata.txt 在这儿（unpack = false，没解开）
#   $WORK       容器内工作区

build() {
  : # 没有东西需要编译——生成脚本是 Python，PEM 是文本
}

package() {
  d="$PKGDEST"

  # ★ 布局照 Alpine 的 ca-certificates-bundle。实测它提供三样东西：
  #
  #     etc/ssl/certs/ca-certificates.crt   ← 真正的文件（189 KB）
  #     etc/ssl/cert.pem                    ← 符号链接，指向上面那个
  #     etc/ssl1.1/…                        ← OpenSSL 1.1 的老路径，不要
  #
  # 而 OpenSSL 1.1 之后默认在 OPENSSLDIR 下找 cert.pem 和 certs/，
  # 所以这两个路径就够——apk 的 libfetch 走的就是 OpenSSL 的默认。
  mkdir -p "$d/etc/ssl/certs"

  python3 "$PKGDIR/make-ca-bundle.py" "$SRCDIR/certdata.txt" \
      "$d/etc/ssl/certs/ca-certificates.crt"

  # /etc/ssl/cert.pem 是个符号链接（相对路径——绝对路径会在 chroot 之外
  # 检查时指向宿主的 /etc/ssl）。
  ln -s certs/ca-certificates.crt "$d/etc/ssl/cert.pem"

  # ★ 这个包属于「升级会覆盖本地修改」的那一类：手工往里加证书没有意义，
  #   因为下次升级就没了。写进 protected_paths 会让 apk 拒绝覆盖它——
  #   那是反的。所以【不写】。
  #
  #   真要让本地证书生效，那是 /usr/local/share/ca-certificates 的事，
  #   需要 update-ca-certificates 那个工具（这个包不含它）。
}
