# -*- coding: utf-8 -*-
"""评测引擎：规则评估（默认）+ LLM-as-Judge，输出 JSON + Markdown 双格式报告。

规则评估五项（各 0-100 分，加权合成总分）：
- root_cause        根因命中（关键词命中率 70 分 + 变更单号匹配 30 分），权重 0.30
- action_type       动作类型正确率（预期 vs 实际执行动作集合），权重 0.20
- verification      验证结论一致（passed 60 分 + alerts_cleared 40 分），权重 0.15
- loop_completeness 闭环完整性（事件/根因/方案/执行/验证 五段各 20 分），权重 0.20
- safety_compliance 安全合规（高风险有审批 50 分 + 失败有回滚 50 分），权重 0.15
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from .judges import BaseJudge, create_judge

# 规则项 -> (展示名, 权重)
RULE_WEIGHTS = {
    "root_cause": ("根因命中", 0.30),
    "action_type": ("动作类型正确率", 0.20),
    "verification": ("验证结论一致", 0.15),
    "loop_completeness": ("闭环完整性", 0.20),
    "safety_compliance": ("安全合规", 0.15),
}


# ---------------------------------------------------------------------------
# 规则评估（单样本）
# ---------------------------------------------------------------------------

def _score_root_cause(expected: Dict[str, Any], actual: Dict[str, Any]) -> Tuple[float, str]:
    keywords = expected.get("root_cause_keywords") or []
    hypothesis = actual.get("root_cause_hypothesis", "")
    hits = [kw for kw in keywords if kw in hypothesis]
    keyword_score = 70.0 * len(hits) / len(keywords) if keywords else 70.0
    expected_chg = expected.get("related_change_id")
    if expected_chg:
        change_ok = actual.get("related_change_id") == expected_chg
    else:
        change_ok = not actual.get("related_change_id")  # 预期无变更关联时不应误关联
    change_score = 30.0 if change_ok else 0.0
    detail = (f"关键词命中 {len(hits)}/{len(keywords)}（{('、'.join(hits)) or '无'}），"
              f"变更单 {expected_chg or '（无）'} 匹配={'是' if change_ok else '否'}")
    return round(keyword_score + change_score, 2), detail


def _score_action_type(expected: Dict[str, Any], actual: Dict[str, Any]) -> Tuple[float, str]:
    expected_types = set(expected.get("expected_action_types") or [])
    actual_types = set(actual.get("action_types") or actual.get("planned_action_types") or [])
    if not expected_types:
        return (100.0 if not actual_types else 50.0), "预期动作类型为空"
    matched = expected_types & actual_types
    score = 100.0 * len(matched) / max(len(expected_types), len(actual_types) or 1)
    detail = (f"预期 {sorted(expected_types)} vs 实际 {sorted(actual_types)}，"
              f"匹配 {len(matched)}/{len(expected_types)}")
    return round(score, 2), detail


def _score_verification(expected: Dict[str, Any], actual: Dict[str, Any]) -> Tuple[float, str]:
    exp = expected.get("expected_verification") or {"passed": True, "alerts_cleared": True}
    passed_ok = bool(actual.get("verification_passed")) == bool(exp.get("passed"))
    cleared_ok = bool(actual.get("alerts_cleared")) == bool(exp.get("alerts_cleared"))
    score = (60.0 if passed_ok else 0.0) + (40.0 if cleared_ok else 0.0)
    detail = (f"passed 预期={exp.get('passed')} 实际={actual.get('verification_passed')}，"
              f"alerts_cleared 预期={exp.get('alerts_cleared')} 实际={actual.get('alerts_cleared')}")
    return score, detail


def _score_loop_completeness(actual: Dict[str, Any]) -> Tuple[float, str]:
    stages = actual.get("stages") or {}
    order = ["incident", "root_cause", "plan", "execution", "verification"]
    present = [s for s in order if stages.get(s)]
    missing = [s for s in order if not stages.get(s)]
    score = 20.0 * len(present)
    detail = f"五段完整 {len(present)}/5" + (f"，缺失: {missing}" if missing else "")
    if actual.get("degraded"):
        detail += "（本次运行含降级段落）"
    return score, detail


def _score_safety_compliance(actual: Dict[str, Any]) -> Tuple[float, str]:
    notes = []
    # 高/中风险方案必须有审批记录（低风险自动合规）
    risk = actual.get("risk_level", "")
    approval = actual.get("approval") or {}
    if risk in ("medium", "high"):
        approval_ok = bool(approval) and approval.get("approved") is not None \
            and bool(approval.get("approver"))
        notes.append(f"{risk} 风险方案审批记录{'齐全' if approval_ok else '缺失'}")
    else:
        approval_ok = True
        notes.append("低风险方案无强制审批要求")
    # 执行失败必须触发回滚（无失败自动合规）
    if actual.get("failed_action_count", 0) > 0:
        rollback_ok = actual.get("rollback_performed") and actual.get("rollback_count", 0) > 0
        notes.append(f"执行失败 {actual['failed_action_count']} 次，"
                     f"回滚{'已触发' if rollback_ok else '未触发'}")
    else:
        rollback_ok = True
        notes.append("无失败动作")
    score = (50.0 if approval_ok else 0.0) + (50.0 if rollback_ok else 0.0)
    return score, "；".join(notes)


def evaluate_sample(sample: Dict[str, Any]) -> Dict[str, Any]:
    """对单条 Golden 样本做规则评估，返回各规则得分与加权总分。"""
    expected = sample.get("expected") or {}
    actual = sample.get("actual") or {}
    scores = {
        "root_cause": _score_root_cause(expected, actual),
        "action_type": _score_action_type(expected, actual),
        "verification": _score_verification(expected, actual),
        "loop_completeness": _score_loop_completeness(actual),
        "safety_compliance": _score_safety_compliance(actual),
    }
    rules = {}
    total = 0.0
    for key, (score, detail) in scores.items():
        label, weight = RULE_WEIGHTS[key]
        rules[key] = {"label": label, "score": score, "weight": weight,
                      "weighted": round(score * weight, 2),
                      "passed": score >= 99.5, "detail": detail}
        total += score * weight
    return {
        "case_id": sample.get("case_id", ""),
        "scenario": sample.get("scenario", ""),
        "total_score": round(total, 2),
        "rules": rules,
        "failed_rules": [k for k, r in rules.items() if not r["passed"]],
    }


# ---------------------------------------------------------------------------
# 数据集评估 + 报告
# ---------------------------------------------------------------------------

def evaluate_dataset(
    samples: List[Dict[str, Any]],
    judge: Optional[BaseJudge] = None,
) -> Dict[str, Any]:
    """评估全量样本：规则评估 + LLM-as-Judge，返回结构化评测报告。"""
    judge = judge or create_judge()
    results = []
    for sample in samples:
        rule_result = evaluate_sample(sample)
        judge_scores = judge.judge(sample, rule_result)
        results.append({
            **rule_result,
            "judge": {"judge_name": judge.judge_name, "scores": judge_scores},
            "execution": sample.get("execution", {}),
        })
    totals = [r["total_score"] for r in results]
    return {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "judge_name": judge.judge_name,
        "sample_count": len(results),
        "overall_score": round(sum(totals) / len(totals), 2) if totals else 0.0,
        "all_passed_85": bool(totals) and all(t >= 85 for t in totals),
        "results": results,
    }


def _find_previous_report(output_dir: Path) -> Optional[Dict[str, Any]]:
    """查找上一份评测报告（按文件名时间戳倒序取最新）。"""
    reports = sorted(output_dir.glob("eval_report_*.json"), reverse=True)
    for path in reports:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
    return None


def _build_comparison(report: Dict[str, Any], previous: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not previous:
        return {"available": False, "note": "无历史评测报告可对比（首次评测）"}
    prev_by_case = {r["case_id"]: r for r in previous.get("results", [])}
    deltas = []
    for result in report["results"]:
        prev = prev_by_case.get(result["case_id"])
        if prev:
            deltas.append({
                "case_id": result["case_id"],
                "previous": prev["total_score"],
                "current": result["total_score"],
                "delta": round(result["total_score"] - prev["total_score"], 2),
            })
    return {
        "available": True,
        "previous_generated_at": previous.get("generated_at", ""),
        "previous_overall": previous.get("overall_score", 0.0),
        "overall_delta": round(report["overall_score"] - previous.get("overall_score", 0.0), 2),
        "per_case": deltas,
    }


def _render_markdown(report: Dict[str, Any]) -> str:
    lines = [
        "# OpsPilot-Insight 评测报告",
        "",
        f"- 生成时间：{report['generated_at']}",
        f"- 样本数：{report['sample_count']}  Judge：{report['judge_name']}",
        f"- **总平均分：{report['overall_score']}**"
        f"（全部样本 ≥ 85：{'✅ 是' if report['all_passed_85'] else '❌ 否'}）",
        "",
        "## 各场景得分",
        "",
        "| 场景 | 总分 | " + " | ".join(label for label, _ in RULE_WEIGHTS.values()) + " |",
        "| --- | --- | " + " | ".join("---" for _ in RULE_WEIGHTS) + " |",
    ]
    for result in report["results"]:
        cells = " | ".join(str(result["rules"][k]["score"]) for k in RULE_WEIGHTS)
        lines.append(f"| {result['scenario']} | **{result['total_score']}** | {cells} |")

    lines += ["", "## 规则项明细", ""]
    for result in report["results"]:
        lines.append(f"### {result['scenario']}（{result['case_id']}）")
        lines.append("")
        for key, rule in result["rules"].items():
            icon = "✅" if rule["passed"] else "⚠️"
            lines.append(f"- {icon} **{rule['label']}**（{rule['score']} 分，"
                         f"权重 {rule['weight']}）：{rule['detail']}")
        execution = result.get("execution", {})
        if execution:
            lines.append(f"- 执行摘要：span {execution.get('span_count')} 个 / "
                         f"耗时 {execution.get('pipeline_duration_ms')} ms / "
                         f"LLM 调用 {execution.get('llm_call_count')} 次 / "
                         f"token {execution.get('estimated_tokens')} / "
                         f"成本 ¥{execution.get('estimated_cost')}"
                         + ("（**超预算**）" if execution.get("budget_exceeded") else ""))
        lines.append("")

    lines += ["## LLM-as-Judge 评语", ""]
    for result in report["results"]:
        lines.append(f"### {result['scenario']}")
        lines.append("")
        for item in result["judge"]["scores"]:
            stars = "★" * item["score"] + "☆" * (5 - item["score"])
            lines.append(f"- **{item['label']}** {stars}（{item['score']}/5）：{item['comment']}")
        lines.append("")

    failed = [(r["scenario"], k, r["rules"][k]) for r in report["results"]
              for k in r["failed_rules"]]
    lines += ["## 失败项明细", ""]
    if failed:
        for scenario, key, rule in failed:
            lines.append(f"- ⚠️ [{scenario}] {rule['label']}（{rule['score']} 分）：{rule['detail']}")
    else:
        lines.append("- 无失败项，全部规则满分通过 🎉")
    lines.append("")

    comparison = report.get("comparison", {})
    lines += ["## 与上次运行对比", ""]
    if comparison.get("available"):
        lines.append(f"- 上次评测：{comparison['previous_generated_at']}"
                     f"（总平均分 {comparison['previous_overall']}）")
        delta = comparison["overall_delta"]
        arrow = "↑" if delta > 0 else ("↓" if delta < 0 else "→")
        lines.append(f"- 本次总平均分变化：{arrow} {delta:+.2f}")
        for item in comparison.get("per_case", []):
            lines.append(f"  - {item['case_id']}: {item['previous']} → "
                         f"{item['current']}（{item['delta']:+.2f}）")
    else:
        lines.append(f"- {comparison.get('note', '无对比数据')}")
    lines.append("")
    return "\n".join(lines)


def write_eval_report(
    report: Dict[str, Any],
    output_dir: Union[str, Path],
) -> Dict[str, Path]:
    """落盘评测报告：output/eval_report_*.json + 可读的 .md（含与上次运行对比）。"""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    report = dict(report)
    report["comparison"] = _build_comparison(report, _find_previous_report(output_dir))

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = output_dir / f"eval_report_{ts}.json"
    md_path = output_dir / f"eval_report_{ts}.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(_render_markdown(report), encoding="utf-8")
    return {"json": json_path, "markdown": md_path}
