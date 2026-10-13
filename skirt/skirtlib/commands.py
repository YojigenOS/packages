"""子命令实现与 CLI 入口。"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from .common import (BUILD_DIR, CACHE_DIR, LOG_DIR, OUTPUT_DIR, RECIPES_DIR, ROOT,
                     die, log, resolve_arch, rmtree_force)

from .spec import direct_deps, load_spec, recipe_of, subpackages

from .deps import build_sysroot, dep_closure, dep_payload, load_host_provides, source_date_epoch

from .source import fetch, srcrel_of

from .build import make_build_script, run_in_container

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

    【为什么需要它】scripts/run-vm.sh 和 scripts/smoke.sh 要找到产物、源码
    缓存这些东西。原来它们各自写死了 `output/$ARCH/`、`cache/`——于是这些
    路径就有了【两份真相】：驱动里一份（common.py 的常量），脚本里一份。

    2026-10-11 把产物集中到 var/ 下时，这个毛病当场发作了：驱动改了，脚本
    没改，冒烟测试报"缺产物"。

    所以路径只该有一处。脚本这样拿：

        eval "$(./skirt/skirt paths --shell)"

    然后就能用 $OUTPUT_DIR / $CACHE_DIR / $BUILD_DIR / $LOG_DIR。
    """
    if getattr(_args, "shell", False):
        for name, path in (
            ("VAR_DIR", ROOT / "var"), ("OUTPUT_DIR", OUTPUT_DIR),
            ("CACHE_DIR", CACHE_DIR), ("BUILD_DIR", BUILD_DIR), ("LOG_DIR", LOG_DIR),
        ):
            print(f'{name}="{path}"')
    else:
        for name, path in (
            ("var", ROOT / "var"), ("output", OUTPUT_DIR), ("cache", CACHE_DIR),
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

    print()
    run_in_container(srcrel, srcname, arch, spec,
                     source_date_epoch(spec, arch, host_provides),
                     unpack=unpack)

    results = collect(spec, arch)
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
            # 方式失败（实测：`skirt image base-devel` 报"glibc 还没构建"，
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
    from .spec import all_recipe_names, kind_of
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

    p = sub.add_parser("clean", help="清理构建目录 / 指定包的产物")
    p.add_argument("package", nargs="?")
    p.set_defaults(func=cmd_clean)

    args = ap.parse_args()
    args.func(args)


