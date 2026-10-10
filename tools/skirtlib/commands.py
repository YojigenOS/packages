"""子命令实现与 CLI 入口。"""

from __future__ import annotations

import argparse
import shutil

from .common import BUILD_DIR, OUTPUT_DIR, PACKAGES_DIR, ROOT, die, log, resolve_arch, rmtree_force

from .spec import direct_deps, load_spec

from .deps import build_sysroot, dep_closure, dep_payload, load_host_provides

from .source import extract, fetch

from .build import make_build_script, run_in_container

from .collect import collect

# ------------------------------------------------------------------ 命令

def cmd_build(args) -> None:
    arch = resolve_arch(args.arch)
    spec = load_spec(args.package)
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

    # 源码
    print()
    archive = fetch(spec)
    srcrel = extract(archive, BUILD_DIR / "src")

    if steps.is_file():
        shutil.copy2(steps, BUILD_DIR / "steps.sh")
        log("步骤", "已加载 steps.sh")

    # 生成的构建脚本先落成文件再送进容器。这样 steps.sh 里的 heredoc、
    # 引号、变量展开都不必经过任何转义——脚本就是脚本。
    (BUILD_DIR / "run.sh").write_text(make_build_script(spec), encoding="utf-8")

    print()
    run_in_container(srcrel, arch, spec)

    out, count = collect(spec, arch)
    print()
    log("完成", f"{out.relative_to(ROOT)}/  （{count} 项）")


def cmd_deps(args) -> None:
    """只解析依赖，不构建 —— 用来调试配方。"""
    arch = resolve_arch(args.arch)
    spec = load_spec(args.package)
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

    p = sub.add_parser("clean", help="清理构建目录 / 指定包的产物")
    p.add_argument("package", nargs="?")
    p.set_defaults(func=cmd_clean)

    args = ap.parse_args()
    args.func(args)


