"""子命令实现与 CLI 入口。"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from .common import BUILD_DIR, OUTPUT_DIR, PACKAGES_DIR, ROOT, die, log, resolve_arch, rmtree_force

from .spec import direct_deps, load_spec, recipe_of, subpackages

from .deps import build_sysroot, dep_closure, dep_payload, load_host_provides, source_date_epoch

from .source import extract, fetch

from .build import make_build_script, run_in_container

from .collect import collect

from .rootfs import assemble

from .variants import list_variants, load_variant

# ------------------------------------------------------------------ 工具

def _recipe_spec(name: str) -> dict:
    """给 build / deps 用：只接受配方名。

    子包没有自己的配方，它的文件是母配方一次构建出来的——收到子包名时
    说清楚该怎么办，而不是丢一句「找不到配方」让人去猜。
    """
    recipe = recipe_of(name)
    if recipe is not None and recipe != name:
        die(
            f"{name} 是 {recipe} 的子包，不能单独构建。\n"
            f"       它由 {recipe} 一次产出的：tools/skirt build {recipe}"
        )
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
    if spec.get("source"):
        archive = fetch(spec)
        srcrel = extract(archive, BUILD_DIR / "src")
    else:
        (BUILD_DIR / "src").mkdir(parents=True, exist_ok=True)
        srcrel = "."
        log("源码", "无（纯元数据包）")

    if steps.is_file():
        shutil.copy2(steps, BUILD_DIR / "steps.sh")
        log("步骤", "已加载 steps.sh")

    # 整个配方目录也送进容器：步骤脚本可以通过 $PKGDIR 取用其中的补丁、
    # conffiles、静态资源等，不必把内容塞进 steps.sh 的 heredoc。
    shutil.copytree(spec["_dir"], BUILD_DIR / "pkg", dirs_exist_ok=True)

    # 生成的构建脚本先落成文件再送进容器。这样 steps.sh 里的 heredoc、
    # 引号、变量展开都不必经过任何转义——脚本就是脚本。
    (BUILD_DIR / "run.sh").write_text(make_build_script(spec), encoding="utf-8")

    print()
    run_in_container(srcrel, arch, spec, source_date_epoch(spec, arch, host_provides))

    results = collect(spec, arch)
    print()
    for out, count in results:
        log("完成", f"{_shown(out)}/  （{count} 项）")


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


def cmd_list(args) -> None:
    if not PACKAGES_DIR.is_dir():
        print("(还没有 packages/ 目录)")
        return
    names = sorted(
        p.name for p in PACKAGES_DIR.iterdir()
        if p.is_dir() and (p / "spec.toml").is_file()
    )
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


def _assemble(packages: list[str], arch: str, output, title: str) -> None:
    """拼装并汇报。rootfs 和 image 只差在包列表从哪来。"""
    print()
    log("拼装", f"{title} [{arch}]")

    out, order = assemble(packages, arch,
                          Path(output) if output else None)
    for name in order:
        log("包含", name)

    print()
    log("完成", f"{_shown(out)}  （{len(order)} 个包, {out.stat().st_size // 1024} KiB）")


def cmd_rootfs(args) -> None:
    """把若干包拼成一个完整根文件系统，打成 tar。"""
    _assemble(args.packages, resolve_arch(args.arch), args.output, "根文件系统")


def cmd_image(args) -> None:
    """按风味定义拼一个镜像。"""
    arch = resolve_arch(args.arch)
    v = load_variant(args.variant)
    print()
    log("风味", f"{v['name']}  {v.get('description', '')}")

    # 默认产物名带上风味：多个风味不能都叫 yojigenos-rootfs。
    output = args.output or (OUTPUT_DIR / arch / f"yojigenos-{v['name']}-{arch}.tar.gz")
    _assemble(v["packages"], arch, output,
              f"风味 {v['name']}（{len(v['packages'])} 个包）")


def cmd_variants(args) -> None:
    list_variants()


def main() -> None:
    ap = argparse.ArgumentParser(
        prog="skirt",
        description="YojigenOS 构建驱动 —— 短裙里装着四次元空间",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("build", help="构建一个包")
    p.add_argument("package")
    p.add_argument("--arch", help="目标架构（默认当前架构）")
    p.set_defaults(func=cmd_build)

    p = sub.add_parser("deps", help="查看依赖解析结果（不构建）")
    p.add_argument("package")
    p.add_argument("--arch", help="目标架构（默认当前架构）")
    p.set_defaults(func=cmd_deps)

    p = sub.add_parser("list", help="列出所有包")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("rootfs", help="把若干包拼成完整根文件系统（tar）")
    p.add_argument("packages", nargs="+", help="要包含的包（依赖会自动带上）")
    p.add_argument("--arch", help="目标架构（默认当前架构）")
    p.add_argument("--output", help="输出的 tar 路径")
    p.set_defaults(func=cmd_rootfs)

    p = sub.add_parser("image", help="按风味定义拼一个镜像（tar）")
    p.add_argument("variant", help="风味名（variants/<名>.toml）")
    p.add_argument("--arch", help="目标架构（默认当前架构）")
    p.add_argument("--output", help="输出的 tar 路径（默认为 output/<架构>/yojigenos-<风味>-<架构>.tar.gz）")
    p.set_defaults(func=cmd_image)

    p = sub.add_parser("variants", help="列出已定义的风味")
    p.set_defaults(func=cmd_variants)

    p = sub.add_parser("clean", help="清理构建目录 / 指定包的产物")
    p.add_argument("package", nargs="?")
    p.set_defaults(func=cmd_clean)

    args = ap.parse_args()
    args.func(args)


