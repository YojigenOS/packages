"""产物收集：把 $PKGDEST 收成 output/<架构>/<包>-<版本>-<release>/。"""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

from .common import BUILD_DIR, OUTPUT_DIR, rmtree_force, sha256_of

from .spec import deps_for

# ------------------------------------------------------------------ 收集

def collect(spec: dict, arch: str) -> tuple[Path, int]:
    pkg = spec["package"]
    destdir = BUILD_DIR / "dest"
    out = OUTPUT_DIR / arch / f"{pkg['name']}-{pkg['version']}-{pkg['release']}"

    rmtree_force(out)
    payload = out / "payload"
    payload.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(destdir, payload, symlinks=True)

    files = []
    for p in sorted(payload.rglob("*")):
        rel = str(p.relative_to(payload))
        if p.is_symlink():
            files.append({"path": rel, "type": "symlink", "target": os.readlink(p)})
        elif p.is_file():
            files.append({
                "path": rel,
                "type": "file",
                "size": p.stat().st_size,
                "sha256": sha256_of(p),
            })
        elif p.is_dir():
            files.append({"path": rel + "/", "type": "dir"})

    deps = deps_for(spec, arch)
    meta = {
        "name": pkg["name"],
        "version": pkg["version"],
        "release": pkg["release"],
        "arch": arch,
        "description": pkg.get("description", ""),
        "section": pkg.get("section", ""),
        "license": pkg.get("license", ""),
        "url": pkg.get("url", ""),
        "depends": deps.get("runtime", []),
        "build_depends": deps.get("build", []),
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "files": files,
    }
    (out / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return out, len(files)


