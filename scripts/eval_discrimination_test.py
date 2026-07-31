#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""评测区分度验证：好 case（Golden）vs 坏 case（故意错误方案）打分对比。

    python scripts/eval_discrimination_test.py                 # 打分 + 断言 + 落盘报告
    python scripts/eval_discrimination_test.py --no-report     # 只打分与断言，不落盘

目的：证明评测引擎有区分度——同一套五维规则下，正确方案得高分（≥ 90），
故意错误的方案得低分（≤ 50），差距 ≥ 40 分；且每类错误在其对应维度显著扣分。
全程离线（MockJudge），不修改 evaluator.py 评分逻辑。
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_PROJECT_ROOT / "src"))

from opspilot.evaluation.dataset_builder import load_golden_dataset  # noqa: E402
from opspilot.evaluation.evaluator import RULE_WEIGHTS, evaluate_dataset  # noqa: E402
from opspilot.evaluation.judges import MockJudge  # noqa: E402

GOLDEN_PATH = _PROJECT_ROOT / "data" / "golden" / "golden_dataset.jsonl"
BAD_CASES_PATH = _PROJECT_ROOT / "data" / "golden" / "bad_cases.jsonl"
REPORT_PATH = _PROJECT_ROOT / "docs" / "evidence" / "eval_discrimination_report.md"

# 区分度验收线
GOOD_MIN_AVG = 90.0     # 好 case 均分下限
BAD_MAX_AVG = 50.0      # 坏 case 均分上限
MIN_GAP = 40.0          # 好坏均分差距下限
BAD_DIMENSION_MAX = 60.0  # 坏 case 主错误维度得分上限（"显著低"判定线）

# 坏 case 错误类型（case_id 中的类型段）-> 主错误规则维度
BAD_TYPE_TO_RULE = {
    "root_cause_wrong": "root_cause",
    "action_type_wrong": "action_type",
    "verification_mismatch": "verification",
    "loop_incomplete": "loop_completeness",
    "safety_violation": "safety_compliance",
    "multi_dimension_failure": None,  # 综合错误：多维同时显著低
}
_LINE = "═" * 78


def bad_case_error_type(case_id: str) -> str:
    """从坏 case 的 case_id（BAD-<错误类型>-<场景>）解析错误类型。"""
    parts = case_id.split("-", 2)
    return parts[1] if len(parts) >= 2 else ""


def run_discrimination(console: bool = True) -> Dict[str, Any]:
    """对好/坏两组数据集分别评测，返回带断言结论的对比结果。"""
    good_samples = load_golden_dataset(GOLDEN_PATH)
    bad_samples = load_golden_dataset(BAD_CASES_PATH)
    if not good_samples:
        raise FileNotFoundError(f"Golden 数据集为空: {GOLDEN_PATH}")
    if not bad_samples:
        raise FileNotFoundError(f"坏 case 数据集为空: {BAD_CASES_PATH}")

    judge = MockJudge()
    good_report = evaluate_dataset(good_samples, judge=judge)
    bad_report = evaluate_dataset(bad_samples, judge=judge)

    good_avg = good_report["overall_score"]
    bad_avg = bad_report["overall_score"]
    gap = round(good_avg - bad_avg, 2)

    # 每类错误在其主维度上应显著低分（综合错误要求多维同时低）
    dimension_checks = []
    for result in bad_report["results"]:
        err_type = bad_case_error_type(result["case_id"])
        rule_key = BAD_TYPE_TO_RULE.get(err_type)
        if rule_key:
            score = result["rules"][rule_key]["score"]
            dimension_checks.append({
                "case_id": result["case_id"], "error_type": err_type,
                "dimension": rule_key, "label": RULE_WEIGHTS[rule_key][0],
                "score": score, "detectable": score <= BAD_DIMENSION_MAX,
            })
        else:  # 综合错误：至少 3 个维度低于判定线
            low = [k for k in RULE_WEIGHTS
                   if result["rules"][k]["score"] <= BAD_DIMENSION_MAX]
            dimension_checks.append({
                "case_id": result["case_id"], "error_type": err_type,
                "dimension": "multi", "label": "多维综合",
                "score": min(result["rules"][k]["score"] for k in RULE_WEIGHTS),
                "detectable": len(low) >= 3, "low_dimensions": low,
            })

    summary = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "good_report": good_report,
        "bad_report": bad_report,
        "good_avg": good_avg,
        "bad_avg": bad_avg,
        "gap": gap,
        "dimension_checks": dimension_checks,
        "assertions": {
            f"好 case 均分 ≥ {GOOD_MIN_AVG}": good_avg >= GOOD_MIN_AVG,
            f"坏 case 均分 ≤ {BAD_MAX_AVG}": bad_avg <= BAD_MAX_AVG,
            f"好坏差距 ≥ {MIN_GAP}": gap >= MIN_GAP,
            "每类错误对应维度可检出": all(c["detectable"] for c in dimension_checks),
        },
    }
    summary["all_passed"] = all(summary["assertions"].values())

    if console:
        _print_summary(summary)
    return summary


def _score_row(result: Dict[str, Any]) -> str:
    rules = result["rules"]
    return (f"{result['case_id']:<44}{result['total_score']:>7.1f}"
            + "".join(f"{rules[k]['score']:>7.1f}" for k in RULE_WEIGHTS))


def _print_summary(summary: Dict[str, Any]) -> None:
    header = f"{'case_id':<44}{'总分':>7}{'根因':>7}{'动作':>7}{'验证':>7}{'闭环':>7}{'安全':>7}"
    print(f"\n{_LINE}\n  评测区分度验证：好 case vs 坏 case\n{_LINE}")
    print(f"[好 case] {len(summary['good_report']['results'])} 条")
    print(header + "\n" + "─" * 78)
    for result in summary["good_report"]["results"]:
        print(_score_row(result))
    print(f"\n[坏 case] {len(summary['bad_report']['results'])} 条")
    print(header + "\n" + "─" * 78)
    for result in summary["bad_report"]["results"]:
        print(_score_row(result))
    print("─" * 78)
    print(f"好 case 均分 {summary['good_avg']:.2f}  vs  坏 case 均分 "
          f"{summary['bad_avg']:.2f}  差距 {summary['gap']:.2f}")
    for name, passed in summary["assertions"].items():
        print(f"  {'✓' if passed else '✗'} {name}")
    print(_LINE)


def render_markdown(summary: Dict[str, Any]) -> str:
    """渲染好坏对比 Markdown 报告（落盘至 docs/evidence/）。"""
    rule_labels = [label for label, _ in RULE_WEIGHTS.values()]
    table_header = ("| case_id | 类型 | 总分 | " + " | ".join(rule_labels) + " |\n"
                    "| --- | --- | --- | " + " | ".join("---" for _ in rule_labels) + " |")

    def rows(report: Dict[str, Any], kind: str) -> List[str]:
        out = []
        for r in report["results"]:
            cells = " | ".join(str(r["rules"][k]["score"]) for k in RULE_WEIGHTS)
            out.append(f"| {r['case_id']} | {kind} | **{r['total_score']}** | {cells} |")
        return out

    lines = [
        "# 评测区分度验证报告",
        "",
        f"- 生成时间：{summary['generated_at']}",
        f"- 好 case：{len(summary['good_report']['results'])} 条"
        f"（data/golden/golden_dataset.jsonl，来自 3 场景闭环回放）",
        f"- 坏 case：{len(summary['bad_report']['results'])} 条"
        f"（data/golden/bad_cases.jsonl，人工构造的故意错误方案）",
        f"- 评分口径：与 `evaluator.py` 五维规则完全一致（未做任何修改），Judge=mock",
        "",
        "## 结论",
        "",
        f"- 好 case 均分 **{summary['good_avg']}**，坏 case 均分 **{summary['bad_avg']}**，"
        f"差距 **{summary['gap']}** 分",
        "",
        "| 验收断言 | 结果 |",
        "| --- | --- |",
    ]
    for name, passed in summary["assertions"].items():
        lines.append(f"| {name} | {'✅ 通过' if passed else '❌ 未通过'} |")

    lines += ["", "## 好坏 case 得分对比", "", table_header]
    lines += rows(summary["good_report"], "好")
    lines += rows(summary["bad_report"], "坏")

    lines += [
        "",
        "## 坏 case 主错误维度检出情况",
        "",
        "每条坏 case 按其错误类型检查对应维度得分是否显著低"
        f"（判定线：≤ {BAD_DIMENSION_MAX} 分；综合错误要求 ≥ 3 个维度同时低）：",
        "",
        "| case_id | 错误类型 | 主维度 | 维度得分 | 可检出 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for check in summary["dimension_checks"]:
        extra = ""
        if check["dimension"] == "multi":
            extra = f"（低分维度：{'、'.join(check.get('low_dimensions', []))}）"
        lines.append(f"| {check['case_id']} | {check['error_type']} | {check['label']}{extra} | "
                     f"{check['score']} | {'✅' if check['detectable'] else '❌'} |")

    lines += [
        "",
        "## 说明",
        "",
        "- 坏 case 与 Golden 样本结构完全一致（expected 保持正确答案，actual 为故意错误方案），"
        "直接复用 `evaluate_dataset` 评分，未修改评分逻辑；",
        "- 6 类错误覆盖：根因错误 / 动作类型错误 / 验证不一致 / 闭环不完整 / 安全违规 / 多维综合错误；",
        "- 错误方案在真实处置中通常存在连带效应（如根因误判导致动作无效、验证失败），"
        "坏 case 按此建模，因此除主错误维度外部分关联维度亦有扣分；",
        "- 复现：`python scripts/eval_discrimination_test.py`，"
        "或 `pytest tests/test_eval_discrimination.py`。",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="eval_discrimination_test",
        description="评测区分度验证：好 case vs 坏 case 打分对比（全程离线）",
    )
    parser.add_argument("--no-report", action="store_true", help="不落盘 Markdown 报告")
    args = parser.parse_args()

    summary = run_discrimination(console=True)
    if not args.no_report:
        REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        REPORT_PATH.write_text(render_markdown(summary), encoding="utf-8")
        print(f"\n区分度报告: {REPORT_PATH.relative_to(_PROJECT_ROOT)}")
    return 0 if summary["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
