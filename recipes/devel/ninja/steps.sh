# ninja 的构建步骤 —— self-bootstrap，没有 configure/make 那一套。

package() {
  cd "$SRCDIR"

  # ★ --bootstrap 是 ninja 官方提供的"从零编译自己"的方式。
  #
  #   普通的构建流程是：configure.py 生成 build.ninja，然后【用 ninja 跑它】。
  #   而在自举模式下，configure.py 直接调 C++ 编译器把源码编出来——不需要
  #   已有的 ninja。这正是我们能从零建它的原因。
  #
  #   实测：产出约 8.7 MB 的动态链接二进制。
  python3 configure.py --bootstrap

  # 它只产出 ./ninja 一个文件，没有 install 目标。
  install -Dm755 ninja "$PKGDEST/usr/bin/ninja"

  # bash 补全和手册页，有就带上（上游把它们放在 misc/ 里）。
  [ -f misc/ninja.bash-completion ] && \
      install -Dm644 misc/ninja.bash-completion \
          "$PKGDEST/usr/share/bash-completion/completions/ninja" || true
  [ -f misc/ninja.zsh-completion ] && \
      install -Dm644 misc/ninja.zsh-completion \
          "$PKGDEST/usr/share/zsh/site-functions/_ninja" || true
  [ -f misc/ninja.1 ] && \
      install -Dm644 misc/ninja.1 "$PKGDEST/usr/share/man/man1/ninja.1" || true

  # 上游还有个 ninja_syntax.py，是给写 build.ninja 的 Python 脚本用的。
  # 有些项目（包括 meson 之外的一些工具）会 import 它。
  [ -f misc/ninja_syntax.py ] && \
      install -Dm644 misc/ninja_syntax.py \
          "$(python3 -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])' \
             | sed "s|^|$PKGDEST|")/ninja_syntax.py" || true
}
