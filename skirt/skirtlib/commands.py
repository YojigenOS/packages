"""子命令实现与 CLI 入口。"""

from __future__ import annotations

import sys

import argparse
import shutil
from pathlib import Path

from .common import (BUILD_DIR, LOG_DIR, OUTPUT_DIR, PKGS_DIR, RECIPES_DIR,
                     REPO_DIR, ROOT, die, log, resolve_arch, rmtree_force)

from .spec import all_recipe_names, direct_deps, load_spec, recipe_of, subpackages

from .deps import (build_sysroot, dep_closure, dep_payload, load_host_provides,
                   output_dir, source_date_epoch)

from .source import fetch, srcrel_of

from .build import make_build_script, run_in_container

from .buildkey import compute as compute_key, stored_parts, stored_key_of, needs_rebuild
from .collect import collect



# ------------------------------------------------------------------ 工具

def _recipe_spec(name: str) -> dict:
    """给 build / deps 用：只接受配方名。

    子包没有自己的配方，它的文件是母配方一次构建出来的。收到子包名时
    【自动换成母配方】，并说清楚换了什么。

    ★ 为什么是"换"而不是"报错"。

    风味定义（variants/*.toml）列的是【要装什么】，所以里面全是子包名：
    glibc-locales、dash-binsh、glibc-dev…… 而 CI 想「按风味的列表建包」
    时，那份列表没法直接用——每个子包都会被拒，而使用者未必知道哪个名字
    是子包、它的母包叫什么。实测就栽在这儿：

        ── 建 glibc-locales ──
        skirt: 错误 glibc-locales 是 glibc 的子包，不能单独构建。

    ★ 而关键在于：子包【只能】通过母包产出。所以「构建 dash-binsh」的
      唯一合理解释就是「构建出 dash-binsh」——而那需要构建 dash。
      这里不隐瞒这件事，而是把它说出来。
    """
    recipe = recipe_of(name)
    if recipe is not None and recipe != name:
        log("子包", f"{name} 由 {recipe} 产出 —— 按 {recipe} 处理")
        return load_spec(recipe)
    return load_spec(name)      # 不认识的会在这里报「找不到配方」


def _shown(path: Path) -> Path:
    """给日志用的路径：能相对就相对，不能就原样。

    `--output` 可以指到仓库外面（CI 就用 /tmp），这时 relative_to 会抛
    ValueError 而不是返回一个可以打印的东西。
    """
    try:
        return path.relative_to(ROOT)
    except ValueError:
        return path


# ------------------------------------------------------------------ 命令

def cmd_paths(_args) -> None:
    """打印各个工作目录，给 shell 脚本用。

    【为什么需要它】`scripts/` 里的脚本要找到产物、源码缓存这些东西。原来它
    自己写死了 `output/$ARCH/`、`cache/`——于是这些路径就有了【两份真相】：
    驱动里一份（common.py 的常量），脚本里一份。

    2026-10-11 把产物集中到 var/ 下时，这个毛病当场发作了：驱动改了，脚本
    没改，冒烟测试报"缺产物"。

    ★ 那条路的历史：这里原来写的是「scripts/run-vm.sh 和 scripts/smoke.sh」。
      run-vm.sh 先搬去 distro、后又被删掉；smoke.sh 在 2026-10-15 也搬进了
      skirt（`skirt smoke`，它现在直接 import common，不需要问路径了）。

    ★ 所以**现在没有调用方了** —— 而它留着：`scripts/` 下还有 keys.sh 这类
      东西，而"shell 脚本不许自己拼 var/ 下的路径"这条规矩没变（见
      docs/约定/仓库结构.md「路径只该有一处」）。哪天有脚本要这些路径，
      出口在这儿，不用再造一个。

    用法：

        eval "$(./skirt/skirt paths --shell)"

    然后就能用 $VAR_DIR / $OUTPUT_DIR / $BUILD_DIR / $LOG_DIR。
    """
    if getattr(_args, "shell", False):
        for name, path in (
            ("VAR_DIR", ROOT / "var"), ("OUTPUT_DIR", OUTPUT_DIR),
            ("BUILD_DIR", BUILD_DIR), ("LOG_DIR", LOG_DIR),
        ):
            print(f'{name}="{path}"')
    else:
        for name, path in (
            ("var", ROOT / "var"), ("output", OUTPUT_DIR),
            ("work", BUILD_DIR), ("logs", LOG_DIR),
        ):
            print(f"  {name:<10} {path}")


def cmd_build(args) -> None:
    arch = resolve_arch(args.arch)
    spec = _recipe_spec(args.package)
    pkg = spec["package"]
    name, ver, rel = pkg["name"], pkg["version"], pkg["release"]

    steps = spec["_dir"] / "steps.sh"
    kind = spec.get("build", {}).get("type", "custom")
    if kind == "custom" and not steps.is_file():
        die(f"{name}: type = \"custom\" 但没有 steps.sh，没有任何构建步骤")

    print()
    log("开始", f"{name} {ver}-{rel} [{arch}]")

    rmtree_force(BUILD_DIR)
    (BUILD_DIR / "dest").mkdir(parents=True)

    # 依赖
    host_provides = load_host_provides()
    order = dep_closure(spec, arch, host_provides)
    if order:
        log("解析", f"依赖链: {' → '.join(order + [name])}")
    else:
        log("解析", "无自建依赖（全部由宿主提供）")
    build_sysroot(order, arch)

    # 源码。没有 [[source]] 的包（例如 filesystem）不下载任何东西——
    # 它构建的是「系统的形状」而不是软件，所以给它一个空的源码树。
    print()
    (BUILD_DIR / "src").mkdir(parents=True, exist_ok=True)
    # ★ 先给默认值，再进分支。
    #
    #   原来是只在 `if spec.get("source")` 里面赋值，于是【纯元数据包】
    #   （filesystem、pip 这类没有 [[source]] 的）走 else 分支，到了下面
    #   run_in_container(..., unpack=unpack) 就报：
    #
    #       UnboundLocalError: cannot access local variable 'unpack'
    #
    #   ★ 这个错只在 CI 上暴露——本地建 pip 的时候 unpack 那个功能还没加，
    #     等加完就没再建过 pip。所以修法不是"在 else 里补一句"，而是把默认
    #     值放在分支外面：以后再加分支也不会忘。
    unpack = True
    if spec.get("source"):
        archive = fetch(spec, arch)
        # ★ 不在宿主上解压。宿主是 macOS，APFS 默认大小写不敏感，而内核源码
        #   里有 13 组只差大小写的文件（xt_MARK.h / xt_mark.h 之类）——在宿主
        #   上解开就会互相覆盖，实测 13 组全变成 0 组，然后构建在 47 分钟后
        #   才失败，而且报的是「找不到 xt_mark.h」这种指不到根因的错误。
        #
        #   所以这里只做两件不涉及文件系统语义的事：
        #     1. 读 tar 里的名字，算出源码根目录叫什么——名字只是字符串
        #     2. 把 tar 原样递进容器——单个文件，没有大小写问题
        #   解压由容器做，那里是 Linux 原生文件系统。
        # ★ unpack = false：这个 source【不是归档】✗ 它本身就是源码 ✓
        #
        # 起因是 ca-certificates：它的源码是 Mozilla 的 certdata.txt，
        # 一份 1.4 MB 的文本文件。它不是 tar 也不是 zip，所以 srcrel_of
        # 读成员名会失败。
        #
        # ★ 与其把它包成 tar.gz 再托管一份，不如承认「单个文件也可以是源码」
        #   这件事——它是通用的，将来还会有别的（二进制 blob、单个配置…）。
        # spec["source"] 是个数组，第一个是主源（fetch 也只看第一个）。
        unpack = (spec["source"][0].get("unpack", True)
                  if spec.get("source") else True)
        srcrel = srcrel_of(archive) if unpack else "."
        shutil.copy2(archive, BUILD_DIR / "src.tar")
        # ★ 再放一份【保持原文件名】的，硬链接到 src.tar（不占额外空间）。
        #
        # 为什么需要：有些工具【按文件名判断格式】，而且要求名字符合规范。
        #
        #   · pip 看到 .tar 就走 tar 分支解它，于是报
        #       tarfile.ReadError: file could not be opened successfully
        #   · 而 wheel 的文件名还必须在规范的位置上——
        #       meson-1.12.1-py3-none-any.whl
        #       名字-版本-python标签-abi标签-平台标签
        #     叫 src.whl 会被拒：
        #       ERROR: Invalid wheel filename (wrong number of parts): 'src'
        #
        # src.tar 这个名字是历史遗留（那时源码只有 tar）。现在它可能是 zip、
        # 可能是 wheel，名字就成了误导。
        named = BUILD_DIR / archive.name
        if not named.exists():
            named.hardlink_to(BUILD_DIR / "src.tar")
        srcname = archive.name
    else:
        srcrel = "."
        srcname = "src.tar"
        log("源码", "无（纯元数据包）")

    if steps.is_file():
        shutil.copy2(steps, BUILD_DIR / "steps.sh")
        log("步骤", "已加载 steps.sh")

    # 整个配方目录也送进容器：步骤脚本可以通过 $PKGDIR 取用其中的补丁、
    # conffiles、静态资源等，不必把内容塞进 steps.sh 的 heredoc。
    shutil.copytree(spec["_dir"], BUILD_DIR / "pkg", dirs_exist_ok=True)

    # 生成的构建脚本先落成文件再送进容器。这样 steps.sh 里的 heredoc、
    # 引号、变量展开都不必经过任何转义——脚本就是脚本。
    (BUILD_DIR / "run.sh").write_text(make_build_script(spec, arch), encoding="utf-8")

    # ★ build_key —— 在构建【之前】算。它的成分和"这次构建结果如何"无关，
    #   所以放前面不会漏掉什么。（见 buildkey.py 顶上那段。）
    print()
    epoch = source_date_epoch(spec, arch, host_provides)
    dep_keys: list[tuple[str, str]] = []
    for dep in order:
        d, dep_rel = output_dir(dep, arch)
        k = stored_key_of(d)
        if k is None:
            # 拿不准就说拿不准 —— 也正因为如此，上面那个包会被判为"该重建"
            k = f"unknown（{dep_rel} 盘上没有 build_key）"
        dep_keys.append((dep, k))
    bk = compute_key(spec, arch, epoch, dep_keys)
    # ★ 把【构建环境】也打出来。
    #
    #   为什么：那个键里有一项是构建环境镜像的内容 Id，而它变了会让**所有**
    #   包的键都变（于是全部重建）。而那是对的 —— 实测过换环境产物就不同
    #   （13 字节那次）。但"全部重建"这件事必须能一眼看出原因，不然它看起来
    #   就像随机行为。`skirt stale` 会说出是哪一项变的，而这行让人平时就看得见。
    env = str(bk["parts"].get("buildenv", "?"))
    log("身份", f"build_key {bk['key'][:16]}…   构建环境 {env[:19]}…")
    log("身份", "          （skirt stale 看它和盘上那份一不一样）")

    run_in_container(srcrel, srcname, arch, spec, epoch, unpack=unpack)

    results = collect(spec, arch, bk, epoch)
    print()
    for out, count in results:
        log("完成", f"{_shown(out)}/  （{count} 项）")


def cmd_bootstrap(args) -> None:
    """按推导出的顺序建完所有包。

    **顺序是算出来的，不是写着的。** 起因是一个担心：重建顺序只存在于某条
    执行过的命令里。现在它在 order.py 里，从配方的依赖关系推导。

    这就是 docs/指南/自举.md 说的那条路：换掉 SKIRT_BUILDENV 指向自己的产物，
    跑同一条命令，就是一次自举。
    """
    import subprocess
    import sys
    from .order import build_order
    from .common import BUILDENV_IMAGE

    arch = resolve_arch(args.arch)
    order, _ = build_order(arch, args.targets or None)

    print()
    log("自举", f"{len(order)} 个包，构建环境 {BUILDENV_IMAGE}:{arch}")
    print()

    from time import monotonic
    done: list[tuple[str, int, bool]] = []
    total0 = monotonic()

    for i, name in enumerate(order, 1):
        print(f"  [{i:2d}/{len(order)}] {name:<16}", end="", flush=True)
        t0 = monotonic()
        r = subprocess.run([sys.executable, str(ROOT / "skirt" / "skirt"),
                            "build", name, "--arch", arch],
                           capture_output=True, text=True)
        dt = int(monotonic() - t0)
        ok = r.returncode == 0
        done.append((name, dt, ok))
        print(f"{'✅' if ok else '❌'} {dt // 60:2d} 分 {dt % 60:02d} 秒")
        # ★ 把每个包的【完整输出】写下来。
        #
        # 原来只打印最后 8 行，剩下的丢掉——而且失败信息里写「日志在 /tmp」，
        # 实际上 skirt 从来不往 /tmp 写任何东西。**那是一句假话**，代价是
        # 实测一次 x86_64 的 glibc 失败，日志里只剩「合并 9715 项到 /」和
        # 一句失败，完全查不下去，绕了很久才靠别的手段定位。
        #
        # 写进 var/logs/，**不能写 var/work/**——cmd_build 每个包
        # 开头都会 rmtree(BUILD_DIR)，写那儿的日志会被下一个包清掉，只剩最后
        # 一个。也不能写 /tmp：CI 根本不收集它。
        logdir = LOG_DIR
        logdir.mkdir(parents=True, exist_ok=True)
        logfile = logdir / f"{i:02d}-{name}.log"
        logfile.write_text(
            (r.stdout or "") + ("\n--- stderr ---\n" + r.stderr if r.stderr else ""))

        if not ok:
            tail = [l for l in r.stdout.splitlines() if l.strip()][-8:]
            for l in tail:
                print(f"           {l}")
            log("停止", f"{name} 失败。完整日志：{logfile}")
            # 【必须非零退出】原来这里只是 break，于是命令退出码是 0——
            # CI 把它当成功，继续往下跑，然后在一步无关的命令上以一种看不懂的
            # 方式失败（实测：`skirt image devel` 报"glibc 还没构建"，
            # 而真正的原因是这个包）。一个报成功的失败比失败更糟。
            raise SystemExit(1)

    total = int(monotonic() - total0)
    print()
    log("结束", f"{total // 60} 分 {total % 60} 秒，"
                f"成功 {sum(1 for _, _, o in done if o)}/{len(order)}")

    if args.report:
        print()
        print("    从慢到快：")
        for name, dt, ok in sorted(done, key=lambda x: -x[1])[:15]:
            print(f"      {name:<16} {dt // 60:3d} 分 {dt % 60:02d} 秒  "
                  f"{'ok' if ok else 'FAIL'}")


def cmd_order(args) -> None:
    """按依赖算出构建顺序，并说明环断在哪里。"""
    from .order import describe

    describe(resolve_arch(args.arch), args.targets or None)


def cmd_deps(args) -> None:
    """只解析依赖，不构建 —— 用来调试配方。"""
    arch = resolve_arch(args.arch)
    spec = _recipe_spec(args.package)
    name = spec["package"]["name"]
    host_provides = load_host_provides()

    print(f"\n{name} [{arch}]")
    print(f"  直接依赖 : {direct_deps(spec, arch) or '（无）'}")
    print(f"  宿主提供 : {sorted(host_provides) or '（无）'}")
    order = dep_closure(spec, arch, host_provides)
    print(f"  需自建   : {order or '（无）'}")
    print(f"  构建顺序 : {' → '.join(order + [name])}\n")
    if order:
        for dep in order:
            payload, rel = dep_payload(dep, arch)
            print(f"    ✅ {rel:<28} {payload.relative_to(ROOT)}")
        print()


def cmd_restore(args) -> None:
    """把 var/pkgs/<架构>/*.apk 还原成 var/output/<架构>/ 里的 payload。

    ★ 为什么需要它：播种和跳过都只拿回 .apk，没拿回 payload —— 于是会出现
      "包明明在仓库里、`skirt build` 却说依赖还没构建"。细节见 restore.py 顶上
      那段（原来是 scripts/restore-payloads.sh）。
    """
    from .restore import restore

    arch = resolve_arch(args.arch)
    print()
    ok, skip, fail = restore(arch)
    print()
    if fail:
        die(f"{fail} 个包没解开（上面标 ✗ 的那些）——它们的 payload 还是缺的")


def cmd_pack(args) -> None:
    """把一个【已建好的】包打成 .apk。

    ★ 原来是 scripts/pack.sh。搬过来的理由和其它几样一样：**判定要能测**。
      而这个脚本决定的是【交付物的字节】—— mkpkg 收哪些字段、时间戳从哪来、
      属主和能力位怎么恢复、旁路文件叫什么名字 —— 它当时是 442 行 shell，
      改动它会不会改变产物，只能靠人记得。

    拿不到时间戳那种错是【整个停下来】的（不是某个包失败），见 pack.py。
    """
    import os
    from .pack import PackError, pack

    arch = resolve_arch(args.arch)
    sign_key = args.sign_key or os.environ.get("SIGN_KEY") or None
    sign_key = Path(sign_key) if sign_key is not None else None

    try:
        ok, fail, skip = pack(arch=arch, names=args.targets or None,
                              all_=args.all, sign_key=sign_key)
    except PackError as exc:
        die(str(exc))
    if fail:
        die(f"{fail} 个包没打出来（上面标 ✗ 的那些）")


def cmd_deploy(args) -> None:
    """把 var/repo/ 的内容传到线上（默认干跑，--go 才真传）。

    ★ 原来是 scripts/deploy.sh。判据都在 deploy.py 里：只 copy 不 sync、先传包
      后传索引、具名 remote 才读那些 RCLONE_CONFIG_* 环境变量、没装 rclone 时
      退化成容器里的那份。
    """
    from .deploy import deploy

    arch = resolve_arch(args.arch)
    sys.exit(deploy(arch=arch, remote=args.remote, go=args.go))


def cmd_smoke(args) -> None:
    """跑一次真的构建，验证整条链是通的（端到端，几分钟，要 docker）。

    ★ 原来是 scripts/smoke.sh。它【不进】 skirt/tests/ —— 那些是 <1 秒的单元
      测试，而这一条要真的下载、解压、编译、运行。两条路要分得清：

          python3 -m unittest discover -s skirt/tests   # 单元测试
          ./skirt/skirt smoke                           # 端到端（几分钟）

      而它的判定部分（尤其第⑥项那条判据）在 skirtlib/smoke.py 里，由
      test_smoke.py 守着 —— 那是能测的那一半。
    """
    import os
    from .smoke import smoke

    arch = resolve_arch(args.arch or os.environ.get("ARCH"))
    sys.exit(smoke(arch=arch, probe=args.probe,
                   buildenv=args.buildenv or os.environ.get("SKIRT_BUILDENV"),
                   kernel_tar=Path(args.kernel_tar) if args.kernel_tar else None))


def cmd_repo(args) -> None:
    """把一个架构的 .apk 收成一个 apk 软件仓库（索引 + 包）。

    ★ 原来是 scripts/repo.sh。搬过来时两件事变了，都记在 repo.py 顶上：
      ① 没有管道了 —— mkndx 的退出码不再被 grep/sed 吃掉
      ② 没有 shell 了 —— `--pkgname-spec` 里那两个 ${…} 不用再给谁看引号
      另外多了一步"查产物"：索引建完，拿 apk adbdump 把索引和仓库里的包对一遍。
    """
    import os
    from .repo import build_index

    arch = resolve_arch(args.arch)
    pkgs_dir, repo_dir = PKGS_DIR / arch, REPO_DIR / arch

    if not pkgs_dir.is_dir():
        die(f"还没有包（{_shown(pkgs_dir)} 不存在）。"
            f"先 skirt build + skirt pack。")
    count = len(sorted(pkgs_dir.glob("*.apk")))
    if count == 0:
        die(f"{_shown(pkgs_dir)} 里没有 .apk")

    sign_key = args.sign_key or os.environ.get("SIGN_KEY") or None
    if sign_key is not None:
        sign_key = Path(sign_key)
        if not sign_key.is_file():
            die(f"找不到签名密钥: {sign_key}")

    desc = args.desc or f"Yojigen OS {arch} 软件仓库"

    print()
    print(f"  收拢 {count} 个包 → {_shown(repo_dir)}/")

    index = build_index(arch=arch, pkgs_dir=pkgs_dir, repo_dir=repo_dir,
                        desc=desc, sign_key=sign_key)

    print()
    print(f"  ✓ {_shown(index)}  {index.stat().st_size} 字节")
    print()
    print("  本地试一下（起个静态服务，然后从它装）：")
    print(f"      cd {_shown(repo_dir)} && python3 -m http.server 18080")
    print("      apk add --allow-untrusted --repository http://localhost:18080 \\")
    print("              --root /tmp/r --initdb <包名>")
    print()


def cmd_prune(args) -> None:
    """把仓库里过期的包清掉（默认干跑，--go 才真删）。

    ★ 三种范围：`--scope last-version`（默认，每个包名只留最新的）、
      `--scope last-release`（同一个上游版本下只留最新那个修订）、
      `--package <名字>`（只清一个包，应急用）。
      决策记录 0020 把"为什么"写全了；实现和测试在 skirtlib/prune.py。

    ★ 它需要 S3 凭据（列线上要它）。本机没有是正常的——凭据只在 CI 的
      secret 里，所以干跑也在 CI 里跑。
    """
    import os
    from .prune import Remote, run

    arch = resolve_arch(args.arch)
    remote = Remote.from_env(arch)
    sign_key = args.sign_key or os.environ.get("SIGN_KEY") or None
    if sign_key is not None:
        sign_key = Path(sign_key)
        if not sign_key.is_file():
            die(f"找不到签名密钥: {sign_key}")

    run(arch=arch, remote=remote, scope=args.scope, package=args.package,
        go=args.go, sign_key=sign_key, desc=args.desc)


def cmd_provides(args) -> None:
    """输出一组目标【及其依赖闭包】的包名，一行一个。

    ★ 这个命令回答的是一个具体问题：**构建环境里真的有什么？**

    起因是一个真出过的事故。CI 里为了让 `skirt build` 知道"依赖不用重建"，
    把 distro 的 variants/devel.toml 里的包名直接当 SKIRT_PROVIDES 传进来。
    但那份列表是【想要什么】，不是【真的有什么】——它的注释里写着：

        # gmp / mpfr / mpc 是 gcc 的运行时依赖，会自动带进来，不用列。

    于是建 gcc 时报：

        [解析] 依赖链: gmp → mpfr → mpc → gcc
        skirt: 错误 依赖 gmp 还没有构建

    而 gmp 明明就在镜像里（libgmp.so.10、gmp.h 都在）。

    差别在于：**镜像里有的是那个列表的依赖闭包**，不只是列表本身。
    所以这里算闭包——而算出来的东西【就是】镜像里有的。

    ★ 为什么不用空 provides 算：那会成环（glibc → make → glibc）。
      起点用 bootstrap/provides.txt，因为那是"从借来的环境出发"时
      真正由宿主提供的东西——devel 镜像就是在那之上长出来的。
    """
    arch = resolve_arch(args.arch)
    from .order import build_order

    names = args.targets or []
    if not names:
        from .spec import all_recipe_names
        names = all_recipe_names()

    order, _ = build_order(arch, names)
    # ★ 目标和闭包一起输出：镜像里有闭包，也有目标本身。
    #   而 build_order 只给"要建的顺序"，目标自己在里面还是不在，取决于
    #   它是不是别人的依赖——所以显式并上。
    out = sorted(set(order) | set(names))
    for n in out:
        print(n)


def cmd_list(args) -> None:
    if not RECIPES_DIR.is_dir():
        print("(还没有 recipes/ 目录)")
        return
    from .spec import all_recipe_names
    names = all_recipe_names()
    if not names:
        print("(还没有任何包)")
        return
    for n in names:
        pkg = load_spec(n)["package"]
        print(f"  {pkg['name']:<16} {pkg['version']}-{pkg['release']:<5} "
              f"{pkg.get('description', '')}")
        for sub in subpackages(load_spec(n)):
            print(f"    └ {sub['name']:<14} {'':<7} {sub.get('description', '')}")


def _staleness(order: list[str], arch: str, host_provides: set[str],
               ) -> dict[str, list[str]]:
    """整条闭包里，谁该重建 —— 含**被依赖带出来的**那些。

    `order` 必须是拓扑序（`dep_closure` 给的就是），因为这里只往前走一遍：
    **看一个包的时候，它的依赖全都已经判过了。**

    【为什么要递归看闭包】

    一个包的 build_key 里，"依赖"那一项取的是**依赖盘上的 key**。于是有这么
    一个形状：

        你改了 openssl 的配方，而没重建它
        → openssl 盘上的 key 还是旧的
        → 用它的 curl，算出来的 key 也是旧的（因为它读的就是那个旧值）
        → **curl 报"不用重建"** ✗

    而实际上，openssl 一旦重建，它的 key 就变了，curl 的 key 也会跟着变 ——
    所以 curl 该重建。

    **换句话说：那个键的"依赖"那一项，该比的是【依赖现在算的 key】，
    而不是【依赖盘上的 key】。** 而这里用另一种写法得到同一件事：拓扑序往前走，
    谁该重建就把它带出来的后果一起记上。

    ★ 而"被带出来"也要说清是哪一条 —— 不然你只看见"curl 该重建"，
      而看不出来原因在 openssl 上。
    """
    verdict: dict[str, list[str]] = {}
    for name in order:
        spec = load_spec(recipe_of(name) or name)
        sub = dep_closure(spec, arch, host_provides)

        dep_keys = [(d, stored_key_of(output_dir(d, arch)[0]) or "unknown") for d in sub]
        now = compute_key(spec, arch, source_date_epoch(spec, arch, host_provides), dep_keys)
        need, why = needs_rebuild(now, stored_parts(output_dir(name, arch)[0]))

        # ★ 依赖该重建 → 这个也跟着该重建（哪怕它自己算出来的 key 没变）。
        carried = [d for d in sub if verdict.get(d)]
        if carried:
            need = True
            why = list(why) + [f"依赖 {d} 该重建" for d in carried]

        verdict[name] = list(why) if need else []
    return verdict


def cmd_stale(args) -> None:
    """回答一个问题：这些包该不该重建。

    ★ 为什么要有它 —— 这是「改了配方要升 release」那条欠账的解法。

    以前判断"要不要重建"靠的是「var/pkgs 里有没有同名同版本同 release 的
    .apk」，也就是靠人记得升那个号。现在改成比 build_key：**两边都是算出来的**，
    不需要 git 历史，也不需要人记得任何事。

    ★ 而它**递归看闭包**：依赖该重建的话，用它的包也算该重建。理由写在
      `_staleness` 的 docstring 里。

    退出码：**0 = 该重建，1 = 都不用**（像 grep）。可脚本化。
    """
    arch = resolve_arch(getattr(args, "arch", None))
    targets = list(args.targets) or all_recipe_names()
    host_provides = load_host_provides()

    need_list, ok_list = [], []
    for target in targets:
        spec = _recipe_spec(target)
        name = spec["package"]["name"]
        _, rel = output_dir(name, arch)

        # 拓扑序：依赖在前。加上目标自己是最后一个 —— 而目标有可能已经在里面
        # （它是别的目标的依赖），所以先去重再保持顺序。
        order = dep_closure(spec, arch, host_provides)
        chain = list(dict.fromkeys(order + [name]))
        verdict = _staleness(chain, arch, host_provides)

        why = verdict.get(name, [])
        if why:
            need_list.append(name)
            print(f"  该重建  {name:<18} {rel}")
            for w in why:
                print(f"          {w}")
        else:
            ok_list.append(name)
            print(f"  不用    {name:<18} {rel}   build_key 没变")

    print()
    print(f"  该重建 {len(need_list)} 个 · 不用 {len(ok_list)} 个")
    sys.exit(0 if need_list else 1)


def cmd_clean(args) -> None:
    if BUILD_DIR.exists():
        rmtree_force(BUILD_DIR)
        log("清理", "build/")
    if args.package and OUTPUT_DIR.is_dir():
        for arch_dir in sorted(OUTPUT_DIR.iterdir()):
            if not arch_dir.is_dir():
                continue
            for d in sorted(arch_dir.glob(f"{args.package}-*")):
                rmtree_force(d)
                log("清理", f"{d.relative_to(ROOT)}/")
    if not args.package:
        log("提示", "未指定包名，只清理了 build/（产物保留）")



def main() -> None:
    ap = argparse.ArgumentParser(
        prog="skirt",
        description="Yojigen OS 构建驱动 —— 短裙里装着四次元空间",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("build", help="构建一个包")
    p.add_argument("package")
    p.add_argument("--arch", help="目标架构（默认当前架构）")
    p.set_defaults(func=cmd_build)

    p = sub.add_parser("bootstrap", help="按推导出的顺序建完所有包（顺序是算的）")
    p.add_argument("targets", nargs="*", help="只建这些包及其依赖闭包")
    p.add_argument("--arch", default=None,
                   help="默认是宿主架构（多平台容器，不需要交叉编译）")
    p.add_argument("--report", action="store_true", help="结束时打印耗时表")
    p.set_defaults(func=cmd_bootstrap)

    p = sub.add_parser("order", help="按依赖算出构建顺序（说明环断在哪里）")
    p.add_argument("targets", nargs="*",
                   help="只排这些包及其依赖闭包（默认全部）")
    p.add_argument("--arch", default=None,
                   help="默认是宿主架构（多平台容器，不需要交叉编译）")
    p.set_defaults(func=cmd_order)

    p = sub.add_parser("deps", help="查看依赖解析结果（不构建）")
    p.add_argument("package")
    p.add_argument("--arch", help="目标架构（默认当前架构）")
    p.set_defaults(func=cmd_deps)

    p_paths = sub.add_parser("paths", help="打印各个工作目录（给脚本用）")

    p_paths.add_argument("--shell", action="store_true", help="输出可直接 eval 的 shell 赋值")

    p_paths.set_defaults(func=cmd_paths)

    p = sub.add_parser("provides",
                       help="输出一组目标的依赖闭包（= 构建环境里真的有什么）")
    p.add_argument("targets", nargs="*", help="默认全部配方")
    p.add_argument("--arch", default=None)
    p.set_defaults(func=cmd_provides)

    p = sub.add_parser("list", help="列出所有包")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("restore",
                       help="把 var/pkgs/<架构>/*.apk 还原成 payload（播种/跳过缺的那一步）")
    p.add_argument("--arch", help="目标架构（默认当前架构）")
    p.set_defaults(func=cmd_restore)

    p = sub.add_parser("pack",
                       help="把一个已建好的包打成 .apk（apk mkpkg + 签名 + 能力位）")
    p.add_argument("targets", nargs="*",
                   help="包名（每个名字取它最新那份产物）；不传要配 --all")
    p.add_argument("--all", action="store_true",
                   help="打 var/output/<架构>/ 下的全部产物")
    p.add_argument("--arch", help="目标架构（默认当前架构）")
    p.add_argument("--sign-key",
                   help="用它签名（包建出来就是签好的；不传就不签。SIGN_KEY 环境变量同理）")
    p.set_defaults(func=cmd_pack)

    p = sub.add_parser("deploy",
                       help="把 var/repo/<架构>/ 传到线上（默认干跑，--go 才真传）")
    p.add_argument("remote", nargs="?",
                   help="rclone 的 <remote>:<桶路径>；没给就用环境变量拼（yojigen:<S3_BUCKET>）")
    p.add_argument("--arch", help="目标架构（默认当前架构）")
    p.add_argument("--go", action="store_true",
                   help="真的传（默认只打印会传什么）")
    p.set_defaults(func=cmd_deploy)

    p = sub.add_parser("smoke",
                       help="跑一次真的构建，验证整条链是通的（端到端，几分钟）")
    p.add_argument("--arch", default=None,
                   help="默认是宿主架构（也可以用 ARCH 环境变量）")
    p.add_argument("--probe", default="hello", help="用哪个包当探针（默认 hello）")
    p.add_argument("--buildenv", default=None,
                   help="用哪个构建环境镜像（默认 SKIRT_BUILDENV 或 yojigenos/devel）")
    p.add_argument("--kernel-tar", default=None,
                   help="内核 tarball 的路径（第①项要用；没有就跳过那一项）")
    p.set_defaults(func=cmd_smoke)

    p = sub.add_parser("repo",
                       help="把 var/pkgs/<架构>/*.apk 收成 apk 软件仓库（索引 + 包）")
    p.add_argument("--arch", help="目标架构（默认当前架构）")
    p.add_argument("--sign-key", help="用它签索引（不传就不签，装的时候 --allow-untrusted）")
    p.add_argument("--desc", help="仓库描述（写进索引）")
    p.set_defaults(func=cmd_repo)

    p = sub.add_parser("stale", help="哪些包该重建（比 build_key，退出码 0=该重建）")
    p.add_argument("targets", nargs="*", help="默认全部配方")
    p.add_argument("--arch", default=None)
    p.set_defaults(func=cmd_stale)

    p = sub.add_parser("prune",
                       help="清掉仓库里过期的包（默认干跑，--go 才真删）")
    p.add_argument("--arch", help="目标架构（默认当前架构）")
    p.add_argument("--scope", default="last-version",
                   choices=("last-version", "last-release"),
                   help="last-version=每个包名只留最新（默认）· "
                        "last-release=同一个上游版本下只留最新那个修订")
    p.add_argument("--package", help="只清这一个包名（应急用）")
    p.add_argument("--go", action="store_true",
                   help="真的删（默认只打印会删什么）")
    p.add_argument("--sign-key", help="重建索引时用它签（和 skirt repo 一样）")
    p.add_argument("--desc", help="重建索引时的仓库描述（默认沿用线上那份）")
    p.set_defaults(func=cmd_prune)

    p = sub.add_parser("clean", help="清理构建目录 / 指定包的产物")
    p.add_argument("package", nargs="?")
    p.set_defaults(func=cmd_clean)

    args = ap.parse_args()
    args.func(args)


