# -*- coding: utf-8 -*-
"""评测引擎与成本追踪。

- dataset_builder：从 output/ 产物自动构建 Golden Dataset（data/golden/*.jsonl）；
- evaluator + judges：规则评估（默认）+ LLM-as-Judge（Mock 默认 / DashScope 就绪）；
- cost：LLM 成本三维分解（per-Agent / per-Skill / per-Model）、预算控制与跨运行趋势。

本包仅依赖标准库与项目内模块，核心流程零新增第三方依赖。
"""

from .cost import (
    aggregate_cost_trend,
    compute_cost_section,
    load_pricing,
)
from .dataset_builder import build_golden_dataset, load_golden_dataset
from .evaluator import evaluate_dataset, evaluate_sample, write_eval_report
from .judges import create_judge

__all__ = [
    "load_pricing", "compute_cost_section", "aggregate_cost_trend",
    "build_golden_dataset", "load_golden_dataset",
    "evaluate_sample", "evaluate_dataset", "write_eval_report",
    "create_judge",
]
