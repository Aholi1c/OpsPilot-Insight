# -*- coding: utf-8 -*-
"""Golden Dataset 自动构建：从 output/ 的 report + trace + metrics 产物提取评测样本。

样本结构（data/golden/golden_dataset.jsonl，每行一条）：
- case_id   ：GOLDEN-<scenario>（同 case 幂等更新，一个场景一条）
- scenario  ：场景名
- input     ：告警摘要（事件标题/严重度/告警组概览）
- expected  ：标准答案。优先取场景目录下人工校准的 expected.json（curated）；
              缺失时从本次 report 提取后固化（derived），后续构建不覆盖 curated
- actual    ：本次运行的实际结果（根因/变更单/动作类型/执行状态/验证结论等）
- execution ：执行摘要（span 数、耗时、LLM 调用数、token 估算、成本估算）

CLI：PYTHONPATH=src python -m opspilot.evaluation.build_dataset
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_GOLDEN_PATH = _PROJECT_ROOT / "data" / "golden" / "golden_dataset.jsonl"


# ---------------------------------------------------------------------------
# output/ 产物扫描
# ---------------------------------------------------------------------------

def collect_runs(output_dir: Union[str, Path]) -> List[Dict[str, Any]]:
    """扫描 output/，把 report 与同 trace_id 的 trace/metrics 配对成运行记录。"""
    output_dir = Path(output_dir)
    runs: List[Dict[str, Any]] = []
    for report_path in sorted(output_dir.glob("incident_report_*.json")):
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        trace_id = report.get("trace_id", "")
        trace = _load_json(output_dir / f"trace_{trace_id}.json")
        metrics = _load_json(output_dir / f"metrics_{trace_id}.json")
        runs.append({
            "report": report, "trace": trace, "metrics": metrics,
            "report_path": report_path,
            "generated_at": report.get("generated_at", ""),
        })
    return runs


def _load_json(path: Path) -> Optional[Dict[str, Any]]:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


# ---------------------------------------------------------------------------
# 字段提取
# ---------------------------------------------------------------------------

def extract_expected(scenario_dir: Path, report: Dict[str, Any]) -> Dict[str, Any]:
    """expected 优先级：场景 expected.json（curated）> 从 report 提取固化（derived）。"""
    curated_path = scenario_dir / "expected.json"
    if curated_path.exists():
        data = json.loads(curated_path.read_text(encoding="utf-8"))
        data.pop("description", None)
        return {**data, "source": "curated"}

    # 首次从运行产物提取后固化（无人工校准文件时的兜底口径）
    selected = report.get("selected_root_cause") or {}
    plan = report.get("remediation_plan") or {}
    execution = report.get("execution_result") or {}
    verification = report.get("verification_result") or {}
    keywords = [w for w in (selected.get("service"), selected.get("category")) if w]
    return {
        "root_cause_keywords": keywords,
        "related_change_id": selected.get("related_change_id"),
        "expected_action_types": [s.get("action_type") for s in plan.get("steps", [])
                                  if s.get("action_type")],
        "expected_execution_status": [execution.get("status", "success")],
        "expected_verification": {
            "passed": bool(verification.get("passed")),
            "alerts_cleared": bool(verification.get("alerts_cleared")),
        },
        "source": "derived",
    }


def extract_actual(report: Dict[str, Any]) -> Dict[str, Any]:
    """从 report 提取本次运行的实际结果（评测引擎的打分对象）。"""
    selected = report.get("selected_root_cause") or {}
    plan = report.get("remediation_plan") or {}
    execution = report.get("execution_result") or {}
    verification = report.get("verification_result") or {}
    approval = execution.get("approval") or {}
    return {
        "root_cause_hypothesis": selected.get("hypothesis", ""),
        "root_cause_category": selected.get("category", ""),
        "related_change_id": selected.get("related_change_id"),
        "confidence": selected.get("confidence", 0.0),
        "action_types": [a.get("action_type") for a in execution.get("actions", [])
                         if a.get("action_type")],
        "planned_action_types": [s.get("action_type") for s in plan.get("steps", [])
                                 if s.get("action_type")],
        "risk_level": plan.get("risk_level", ""),
        "execution_status": execution.get("status", ""),
        "executed": bool(execution.get("executed")),
        "approval": {
            "required": approval.get("required"),
            "approved": approval.get("approved"),
            "approver": approval.get("approver", ""),
        } if approval else None,
        "rollback_performed": bool(execution.get("rollback_performed")),
        "rollback_count": len(execution.get("rollbacks", [])),
        "failed_action_count": sum(
            1 for a in execution.get("actions", []) if a.get("status") == "failed"),
        "verification_passed": bool(verification.get("passed")),
        "alerts_cleared": bool(verification.get("alerts_cleared")),
        "verification_summary": verification.get("summary", ""),
        "postmortem_present": report.get("postmortem") is not None,
        "postmortem_narrative": (report.get("postmortem") or {}).get("narrative", ""),
        "degraded": bool(report.get("degraded")),
        "stages": {
            "incident": report.get("incident") is not None,
            "root_cause": bool(selected),
            "plan": bool(plan),
            "execution": bool(execution),
            "verification": bool(verification),
        },
    }


def _extract_execution_summary(run: Dict[str, Any]) -> Dict[str, Any]:
    """执行摘要：span 数、耗时、LLM 调用数、token 估算、成本估算。"""
    trace = run.get("trace") or {}
    metrics = run.get("metrics") or {}
    llm = metrics.get("llm") or {}
    cost = metrics.get("cost") or {}
    return {
        "trace_id": trace.get("trace_id", run["report"].get("trace_id", "")),
        "span_count": trace.get("span_count", 0),
        "pipeline_duration_ms": metrics.get("pipeline_duration_ms", 0.0),
        "llm_call_count": llm.get("call_count", 0),
        "estimated_tokens": (llm.get("estimated_prompt_tokens", 0)
                             + llm.get("estimated_completion_tokens", 0)),
        "estimated_cost": cost.get("total_cost", 0.0),
        "budget_exceeded": (cost.get("budget") or {}).get("exceeded", False),
    }


def build_sample(run: Dict[str, Any], scenarios_dir: Path) -> Dict[str, Any]:
    """把一次运行记录转换为 Golden Dataset 样本。"""
    report = run["report"]
    scenario = report.get("scenario", "unknown")
    incident = report.get("incident") or {}
    groups = incident.get("alert_groups", [])
    return {
        "case_id": f"GOLDEN-{scenario}",
        "scenario": scenario,
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source_report": run["report_path"].name,
        "input": {
            "title": incident.get("title", ""),
            "severity": incident.get("severity", ""),
            "raw_alert_count": incident.get("raw_alert_count", 0),
            "alert_groups": [
                f"[{g.get('severity')}] {g.get('alertname')} @ {g.get('service')}"
                f" ×{g.get('count')}" for g in groups
            ],
            "summary": incident.get("summary", ""),
        },
        "expected": extract_expected(scenarios_dir / scenario, report),
        "actual": extract_actual(report),
        "execution": _extract_execution_summary(run),
    }


# ---------------------------------------------------------------------------
# 落盘（同 case 幂等更新）
# ---------------------------------------------------------------------------

def load_golden_dataset(golden_path: Union[str, Path] = DEFAULT_GOLDEN_PATH) -> List[Dict[str, Any]]:
    """读取 Golden Dataset（JSONL），文件缺失返回空列表。"""
    golden_path = Path(golden_path)
    if not golden_path.exists():
        return []
    samples = []
    for line in golden_path.read_text(encoding="utf-8").strip().splitlines():
        if line.strip():
            samples.append(json.loads(line))
    return samples


def build_golden_dataset(
    output_dir: Union[str, Path],
    scenarios_dir: Union[str, Path, None] = None,
    golden_path: Union[str, Path] = DEFAULT_GOLDEN_PATH,
) -> List[Dict[str, Any]]:
    """扫描 output/ 增量构建 Golden Dataset：同场景取最新一次运行，同 case 幂等更新。

    curated 的 expected 始终以场景 expected.json 为准；旧样本中 derived 的 expected
    在有更新运行时同步重建。返回排序后的全量样本列表。
    """
    scenarios_dir = Path(scenarios_dir) if scenarios_dir else _PROJECT_ROOT / "examples" / "scenarios"
    runs = collect_runs(output_dir)

    # 同场景仅保留最新一次运行（按 report.generated_at 排序）
    latest: Dict[str, Dict[str, Any]] = {}
    for run in runs:
        scenario = run["report"].get("scenario", "unknown")
        if scenario not in latest or run["generated_at"] >= latest[scenario]["generated_at"]:
            latest[scenario] = run

    existing = {s["case_id"]: s for s in load_golden_dataset(golden_path)}
    for scenario, run in latest.items():
        sample = build_sample(run, scenarios_dir)
        existing[sample["case_id"]] = sample  # 幂等：同 case_id 直接覆盖更新

    samples = sorted(existing.values(), key=lambda s: s["case_id"])
    golden_path = Path(golden_path)
    golden_path.parent.mkdir(parents=True, exist_ok=True)
    golden_path.write_text(
        "".join(json.dumps(s, ensure_ascii=False) + "\n" for s in samples), encoding="utf-8",
    )
    return samples
