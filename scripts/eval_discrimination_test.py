#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""评测区分度验证：好 case（Golden）vs 坏 case（故意错误方案）打分对比。

    python scripts/eval_discrimination_test.py                 # 打分 + 断言 + 落盘报告
    python scripts/eval_discrimination_test.py --no-report     # 只打分与断言，不落盘

目的：证明评测引擎有区分度——同一套五维规则下，正确方案得高分（≥ 90），
故意错误的方案得低分（≤ 50），差距 ≥ 40 分；且每类错误在其对应维度显著扣分。

此外做"单维度隔离扰动"验证：从 Golden 样本出发，每次只篡改某一个维度所读取的
输入字段，校验该维度明显掉分、其余四个维度分值与未扰动时完全一致，从而证明五个
维度各自独立评分、互不串扰（bad_cases.jsonl 建模的是真实处置中的多维连带效应，
两者互补）。

全程离线（MockJudge），不修改 evaluator.py 评分逻辑。
"""
from __future__ import annotations

import argparse
import copy
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Tuple

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_PROJECT_ROOT / "src"))

from opspilot.evaluation.dataset_builder import load_golden_dataset  # noqa: E402
from opspilot.evaluation.evaluator import (  # noqa: E402
    RULE_WEIGHTS, evaluate_dataset, evaluate_sample,
)
from opspilot.evaluation.judges import MockJudge  # noqa: E402

GOLDEN_PATH = _PROJECT_ROOT / "data" / "golden" / "golden_dataset.jsonl"
BAD_CASES_PATH = _PROJECT_ROOT / "data" / "golden" / "bad_cases.jsonl"
REPORT_PATH = _PROJECT_ROOT / "docs" / "evidence" / "eval_discrimination_report.md"

# 区分度验收线
GOOD_MIN_AVG = 90.0     # 好 case 均分下限
BAD_MAX_AVG = 50.0      # 坏 case 均分上限
MIN_GAP = 40.0          # 好坏均分差距下限
BAD_DIMENSION_MAX = 60.0  # 坏 case 主错误维度得分上限（"显著低"判定线）
ISOLATED_TARGET_MAX = 80.0  # 单维度扰动后，被扰动维度得分上限（"明显掉分"判定线）

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


# --------------------------------------------------------------------------- #
# 单维度隔离扰动：每个函数只改写目标维度在 evaluator 中读取的输入字段，
# actual 中的其余字段以及 expected 一律保持不动。
# --------------------------------------------------------------------------- #

def _mutate_root_cause(actual: Dict[str, Any]) -> None:
    """根因维度：假设文本不含任何预期关键词，且误关联一个不存在的变更单。"""
    actual["root_cause_hypothesis"] = "疑似机房整体断电导致全局不可用"
    actual["related_change_id"] = "CHG-00000000-0000"


def _mutate_action_type(actual: Dict[str, Any]) -> None:
    """动作维度：执行与规划动作替换为白名单外的错误动作。"""
    actual["action_types"] = ["restart_database"]
    actual["planned_action_types"] = ["restart_database"]


def _mutate_verification(actual: Dict[str, Any]) -> None:
    """验证维度：验证结论与告警清除状态同时反转。"""
    actual["verification_passed"] = False
    actual["alerts_cleared"] = False


def _mutate_loop_completeness(actual: Dict[str, Any]) -> None:
    """闭环维度：缺失 incident / plan 两段（其余四维均不读取 stages）。"""
    stages = dict(actual.get("stages") or {})
    stages["incident"] = False
    stages["plan"] = False
    actual["stages"] = stages


def _mutate_safety_compliance(actual: Dict[str, Any]) -> None:
    """安全维度：中/高风险方案的审批记录缺失。"""
    actual["approval"] = {}


# 规则维度 -> (扰动说明, 扰动函数)
DIMENSION_MUTATIONS: Dict[str, Tuple[str, Callable[[Dict[str, Any]], None]]] = {
    "root_cause": ("根因假设不含预期关键词且误关联变更单", _mutate_root_cause),
    "action_type": ("执行动作替换为白名单外的错误动作", _mutate_action_type),
    "verification": ("验证结论与告警清除状态反转", _mutate_verification),
    "loop_completeness": ("闭环缺失 incident / plan 两段", _mutate_loop_completeness),
    "safety_compliance": ("中高风险方案审批记录缺失", _mutate_safety_compliance),
}


def bad_case_error_type(case_id: str) -> str:
    """从坏 case 的 case_id（BAD-<错误类型>-<场景>）解析错误类型。"""
    parts = case_id.split("-", 2)
    return parts[1] if len(parts) >= 2 else ""


def build_isolated_sample(golden: Dict[str, Any], rule_key: str) -> Dict[str, Any]:
    """基于 Golden 样本构造"仅单维度错误"的扰动样本（expected 完全不动）。"""
    sample = copy.deepcopy(golden)
    sample["case_id"] = f"ISO-{rule_key}-{golden.get('scenario', '')}"
    DIMENSION_MUTATIONS[rule_key][1](sample["actual"])
    return sample


def run_isolation_checks(good_samples: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """逐维度扰动每条 Golden 样本，校验"只有被扰动维度掉分，其余维度分值不变"。"""
    checks = []
    for golden in good_samples:
        baseline = evaluate_sample(golden)["rules"]
        for rule_key, (description, _) in DIMENSION_MUTATIONS.items():
            mutated = evaluate_sample(build_isolated_sample(golden, rule_key))["rules"]
            affected_others = [
                k for k in RULE_WEIGHTS
                if k != rule_key and baseline[k]["score"] != mutated[k]["score"]
            ]
            mutated_score = mutated[rule_key]["score"]
            checks.append({
                "case_id": f"ISO-{rule_key}-{golden.get('scenario', '')}",
                "scenario": golden.get("scenario", ""),
                "dimension": rule_key,
                "label": RULE_WEIGHTS[rule_key][0],
                "description": description,
                "baseline_score": baseline[rule_key]["score"],
                "mutated_score": mutated_score,
                "affected_others": affected_others,
                "isolated": mutated_score <= ISOLATED_TARGET_MAX and not affected_others,
            })
    return checks


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

    isolation_checks = run_isolation_checks(good_samples)

    summary = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "good_report": good_report,
        "bad_report": bad_report,
        "good_avg": good_avg,
        "bad_avg": bad_avg,
        "gap": gap,
        "dimension_checks": dimension_checks,
        "isolation_checks": isolation_checks,
        "assertions": {
            f"好 case 均分 ≥ {GOOD_MIN_AVG}": good_avg >= GOOD_MIN_AVG,
            f"坏 case 均分 ≤ {BAD_MAX_AVG}": bad_avg <= BAD_MAX_AVG,
            f"好坏差距 ≥ {MIN_GAP}": gap >= MIN_GAP,
            "每类错误对应维度可检出": all(c["detectable"] for c in dimension_checks),
            "单维度扰动仅影响对应维度": all(c["isolated"] for c in isolation_checks),
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
    _print_isolation(summary)
    for name, passed in summary["assertions"].items():
        print(f"  {'✓' if passed else '✗'} {name}")
    print(_LINE)


def _print_isolation(summary: Dict[str, Any]) -> None:
    """按维度汇总单维度隔离扰动结果（多个 Golden 样本合并展示）。"""
    checks = summary.get("isolation_checks") or []
    if not checks:
        return
    sample_count = len({c["scenario"] for c in checks})
    print(f"\n[单维度隔离扰动] {sample_count} 个 Golden 样本 × "
          f"{len(DIMENSION_MUTATIONS)} 个维度 = {len(checks)} 次扰动")
    print(f"{'被扰动维度':<12}{'扰动前':>8}{'扰动后':>8}   {'其余四维':<12}扰动说明")
    print("─" * 78)
    for rule_key, (description, _) in DIMENSION_MUTATIONS.items():
        group = [c for c in checks if c["dimension"] == rule_key]
        label = RULE_WEIGHTS[rule_key][0]
        before = "/".join(sorted({f"{c['baseline_score']:.0f}" for c in group}))
        after = "/".join(sorted({f"{c['mutated_score']:.0f}" for c in group}))
        intact = all(not c["affected_others"] for c in group)
        print(f"{label:<12}{before:>8}{after:>8}   "
              f"{'✓ 分值不变' if intact else '✗ 被连带':<12}{description}")
    print("─" * 78)


def render_markdown(summary: Dict[str, Any]) -> str:
    """渲染好坏对比 Markdown 报告（落盘至 docs/evidence/）。"""
    rule_labels = [label for label, _ in RULE_WEIGHTS.values()]
    isolation = summary.get("isolation_checks") or []
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
        f"（data/golden/golden_dataset.jsonl，来自 4 场景闭环回放）",
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
        "## 五维独立性验证（单维度隔离扰动）",
        "",
        "从每条 Golden 样本出发，每次只篡改某一个维度在 `evaluator.py` 中读取的输入字段，"
        "expected 与 actual 其余字段完全不动，观察得分变化：",
        "",
        f"- 共 {len({c['scenario'] for c in isolation})} 个 Golden 样本 × "
        f"{len(DIMENSION_MUTATIONS)} 个维度 = **{len(isolation)} 次扰动**；",
        f"- 判定口径：被扰动维度得分 ≤ {ISOLATED_TARGET_MAX}（明显掉分）"
        "且其余四个维度得分与扰动前**完全一致**；",
        "",
        "| 被扰动维度 | 扰动说明 | 扰动前得分 | 扰动后得分 | 其余四维 | 隔离成立 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for rule_key, (description, _) in DIMENSION_MUTATIONS.items():
        group = [c for c in isolation if c["dimension"] == rule_key]
        if not group:
            continue
        label = RULE_WEIGHTS[rule_key][0]
        before = " / ".join(sorted({f"{c['baseline_score']:.0f}" for c in group}))
        after = " / ".join(sorted({f"{c['mutated_score']:.0f}" for c in group}))
        intact = all(not c["affected_others"] for c in group)
        ok = all(c["isolated"] for c in group)
        lines.append(f"| {label} | {description} | {before} | **{after}** | "
                     f"{'分值不变' if intact else '被连带扣分'} | {'✅' if ok else '❌'} |")

    lines += [
        "",
        "## 说明",
        "",
        "- 坏 case 与 Golden 样本结构完全一致（expected 保持正确答案，actual 为故意错误方案），"
        "直接复用 `evaluate_dataset` 评分，未修改评分逻辑；",
        "- 6 类错误覆盖：根因错误 / 动作类型错误 / 验证不一致 / 闭环不完整 / 安全违规 / 多维综合错误；",
        "- **两类证据分工**：bad_cases.jsonl 按真实处置建模，错误方案通常存在连带效应"
        "（如根因误判导致动作无效、验证失败），因此除主错误维度外部分关联维度亦有扣分；"
        "而上节单维度隔离扰动则从评分输入层面证明五个维度各自只读自己的字段、互不串扰，"
        "两者共同说明分数差距既反映真实质量差异，也不是某单一维度误差放大所致；",
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
