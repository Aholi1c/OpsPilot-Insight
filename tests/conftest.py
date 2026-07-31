# -*- coding: utf-8 -*-
"""pytest 共享配置：统一将 src/ 与 scripts/ 加入模块搜索路径（免安装直接测试）。

各测试文件无需再复制 sys.path 样板；run_demo.py 与 scripts/replay_eval.py
作为独立入口仍保留各自的路径注入。
"""
from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]

for _path in (str(_PROJECT_ROOT / "src"), str(_PROJECT_ROOT / "scripts")):
    if _path not in sys.path:
        sys.path.insert(0, _path)
