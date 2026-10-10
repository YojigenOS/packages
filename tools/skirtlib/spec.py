"""配方：加载 spec.toml、展开 $NAME/$VERSION 占位符、解析依赖字段。"""

from __future__ import annotations

import tomllib

from .common import PACKAGES_DIR, die

# ------------------------------------------------------------------ 配方

def load_spec(name: str) -> dict:
    path = PACKAGES_DIR / name / "spec.toml"
    if not path.is_file():
        die(f"找不到配方: {path}")
    with path.open("rb") as f:
        spec = tomllib.load(f)
    if "package" not in spec:
        die(f"{path}: 缺少 [package] 段")
    for key in ("name", "version"):
        if key not in spec["package"]:
            die(f"{path}: [package] 缺少 {key}")
    spec["_dir"] = path.parent
    return spec


def expand(spec: dict, value):
    """展开 $NAME / $VERSION 占位符。"""
    pkg = spec["package"]
    if isinstance(value, str):
        return (value
                .replace("$NAME", pkg["name"])
                .replace("$VERSION", pkg["version"]))
    if isinstance(value, list):
        return [expand(spec, v) for v in value]
    if isinstance(value, dict):
        return {k: expand(spec, v) for k, v in value.items()}
    return value


def deps_for(spec: dict, arch: str) -> dict:
    """合并通用依赖与架构特定依赖（架构特定值整体覆盖同名键）。"""
    deps = dict(spec.get("dependencies", {}))
    per_arch = deps.pop(arch, None)
    if isinstance(per_arch, dict):
        deps.update(per_arch)
    return deps


def direct_deps(spec: dict, arch: str) -> list[str]:
    """直接依赖 = runtime + build，去重且保持声明顺序。"""
    d = deps_for(spec, arch)
    out: list[str] = []
    for key in ("runtime", "build"):
        for name in d.get(key, []):
            if name not in out:
                out.append(name)
    return out


