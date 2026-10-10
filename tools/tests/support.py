#!/usr/bin/env python3
"""skirt 的单元测试。

跑法:
    python3 -m unittest discover -s tools/tests -v

这些测试不需要 Docker，也不构建任何东西——只覆盖纯逻辑。重点在
deps.dep_closure()：那里编码的是自举语义，是 skirt 里最容易出错、
也最不该出错的部分。三条规则都是踩坑踩出来的：

  1. 构建环境基线（host/provides.txt）里的包，跳过
  2. 自己依赖自己 = 用上一轮构建产物，**不是**成环
  3. 真正的环（glibc → make → glibc）必须报错

哪天重构时改错一条，表现会是「某个包构建出奇怪的结果」而不是报错。
所以它们值得有测试守着。
"""

from __future__ import annotations

import datetime
import re
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from skirtlib import build as build_mod
from skirtlib import collect as collect_mod
from skirtlib import spec as spec_mod
from skirtlib import deps as deps_mod
from skirtlib.build import run_in_container
from skirtlib.common import PACKAGES_DIR, build_jobs

#: 依赖闭包测试要用的基线。直接用真实的 host/provides.txt——不要在这里
#: 另写一份：【另写一份就会漂移】，而它漂移的表现是测试莫名其妙地报环，
#: 或者更糟：报一个已经不存在的环。工具链和 autotools 都在里面，原因见
#: 那个文件自己的注释（它们都是「构建构建系统」的工具，靠排序解不开）。
from skirtlib.deps import load_host_provides as _load_host_provides
TOOLCHAIN_BASELINE = _load_host_provides()
from skirtlib.deps import load_host_provides, merge_tree, source_date_epoch
from skirtlib.spec import (deps_for, direct_deps, expand, is_subpackage,
                           latest_date, load_spec, package_date, recipe_of,
                           subpackages)

# 这个文件里放【共用的辅助】，不放测试。

def _dt(text: str) -> datetime.datetime:
    """把 "2026-10-10T15:30:00Z" 这种写法变成带时区的 datetime。"""
    return datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))

def _pkg(name: str, date, **extra) -> dict:
    return {"package": {"name": name, "version": "1", "release": 1,
                        "date": date, **extra}}

def _all_spec_dirs():
    """所有配方目录。

    目录按 kind 分层（packages/lib/glibc/），所以不能直接 iterdir()。
    走 spec.all_recipe_names() + spec_path()，这样"目录结构长什么样"这件事
    只有 spec.py 一处知道。
    """
    return [spec_mod.spec_path(n).parent for n in spec_mod.all_recipe_names()]
