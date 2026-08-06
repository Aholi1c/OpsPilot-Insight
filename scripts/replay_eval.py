#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回放评测：一键对 4 个内置场景各跑一遍五段闭环并立即评估，生成汇总。

    python scripts/replay_eval.py                    # 4 场景回放 + Golden 构建 + 评测报告
    python scripts/replay_eval.py --output-dir out2  # 指定产物目录
    python scripts/replay_eval.py --scenario container_oom  # 只回放指定场景

全程离线（MockProvider + 自动审批）；回放使用知识库临时拷贝，不污染仓库种子数据。
"""
from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_PROJECT_ROOT / "src"))

from opspilot.evaluation import (  # noqa: E402
    build_golden_dataset, create_judge, evaluate_dataset, write_eval_report,
)
from opspilot.orchestrator import Orchestrator  # noqa: E402

SCENARIOS = ["db_pool_exhaustion", "container_oom", "network_latency", "transaction_risk_surge"]
_LINE = "═" * 78


def run_replay(
    output_dir: Union[str, Path, None] = None,
    scenarios: Optional[List[str]] = None,
    golden_path: Union[str, Path, None] = None,
    console: bool = True,
) -> Dict[str, Any]:
    """回放 + 构建 + 评估的一体化入口（供 CLI 与测试复用），返回评测报告。"""
    output_dir = Path(output_dir) if output_dir else _PROJECT_ROOT / "output"
    scenarios = scenarios or SCENARIOS
    golden_path = golden_path or _PROJECT_ROOT / "data" / "golden" / "golden_dataset.jsonl"

    # ---- 1. 回放：4 场景各跑一遍五段闭环（知识库用临时拷贝，避免案例沉淀污染仓库）----
    tmp_root = Path(tempfile.mkdtemp(prefix="opspilot_replay_"))
    try:
        knowledge_dir = tmp_root / "knowledge"
        shutil.copytree(_PROJECT_ROOT / "data" / "knowledge", knowledge_dir)
        orchestrator = Orchestrator(output_dir=output_dir, console=False,
                                    knowledge_dir=knowledge_dir)
        for scenario in scenarios:
            if console:
                print(f"▶ 回放场景 {scenario} ...", flush=True)
            report = orchestrator.run(scenario)
            if console:
                status = "降级" if report.degraded else "正常"
                print(f"  ✓ 完成（{status}，trace_id={report.trace_id}）")
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)

    # ---- 2. Golden Dataset 构建（同 case 幂等更新）----
    samples = build_golden_dataset(output_dir, golden_path=golden_path)
    picked = [s for s in samples if s["scenario"] in scenarios]
    if console:
        print(f"\n▶ Golden Dataset 构建完成: {golden_path}（{len(samples)} 条样本）")

    # ---- 3. 评测（规则评估 + LLM-as-Judge）并落盘报告 ----
    report = evaluate_dataset(picked or samples, judge=create_judge())
    paths = write_eval_report(report, output_dir)
    report["report_paths"] = {k: str(v) for k, v in paths.items()}

    if console:
        _print_summary(report)
        print(f"\n评测报告: {paths['json'].name} / {paths['markdown'].name}（{output_dir}/）")
    return report


def _print_summary(report: Dict[str, Any]) -> None:
    """控制台汇总表：各场景总分 + 五项规则得分 + 成本。"""
    print(f"\n{_LINE}")
    print("  回放评测汇总")
    print(_LINE)
    header = (f"{'场景':<22}{'总分':>7}{'根因':>7}{'动作':>7}{'验证':>7}"
              f"{'闭环':>7}{'安全':>7}{'成本(¥)':>10}")
    print(header)
    print("─" * 78)
    for result in report["results"]:
        rules = result["rules"]
        cost = result.get("execution", {}).get("estimated_cost", 0.0)
        budget_flag = " ⚠超预算" if result.get("execution", {}).get("budget_exceeded") else ""
        print(f"{result['scenario']:<22}{result['total_score']:>7.1f}"
              f"{rules['root_cause']['score']:>7.1f}{rules['action_type']['score']:>7.1f}"
              f"{rules['verification']['score']:>7.1f}{rules['loop_completeness']['score']:>7.1f}"
              f"{rules['safety_compliance']['score']:>7.1f}{cost:>10.4f}{budget_flag}")
    print("─" * 78)
    print(f"{'总平均分':<22}{report['overall_score']:>7.1f}    "
          f"全部样本 ≥ 85: {'✓ 通过' if report['all_passed_85'] else '✗ 未通过'}")
    print(_LINE)


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="replay_eval",
        description="回放评测：4 场景五段闭环回放 + Golden Dataset 构建 + 评测报告生成（全程离线）",
    )
    parser.add_argument("--output-dir", "-o", default=None, help="产物输出目录（默认 ./output）")
    parser.add_argument("--scenario", "-s", action="append", dest="scenarios",
                        help="只回放指定场景（可多次传入，默认全部 4 个）")
    args = parser.parse_args()

    report = run_replay(output_dir=args.output_dir, scenarios=args.scenarios)
    return 0 if report["all_passed_85"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
