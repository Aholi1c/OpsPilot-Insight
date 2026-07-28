# -*- coding: utf-8 -*-
"""统一数据读取模块：扫描 output/ 产物、加载 golden dataset、聚合指标。

全部操作只读本地文件，不联网。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = PROJECT_ROOT / "output"
GOLDEN_PATH = PROJECT_ROOT / "data" / "golden" / "golden_dataset.jsonl"

# 复用 opspilot 包（成本趋势聚合等），免安装直接读 src/
if str(PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "src"))


def load_json(path: Path) -> Optional[Dict[str, Any]]:
    """安全加载 JSON 文件。"""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def load_jsonl(path: Path) -> List[Dict[str, Any]]:
    """加载 JSONL 文件为列表。"""
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").strip().splitlines():
        if line.strip():
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def list_runs() -> List[Dict[str, Any]]:
    """扫描 output/：把 report 与同 trace_id 的 trace/metrics/audit 配对，按时间倒序。"""
    runs = []
    for report_path in OUTPUT_DIR.glob("incident_report_*.json"):
        report = load_json(report_path)
        if not report:
            continue
        trace_id = report.get("trace_id", "")
        run_ts = "_".join(report_path.stem.split("_")[-2:])
        scenario = report.get("scenario", "")
        generated_at = report.get("generated_at", "")
        runs.append({
            "label": f"{generated_at}  {scenario}  ({trace_id[:8]})",
            "scenario": scenario,
            "generated_at": generated_at,
            "trace_id": trace_id,
            "report": report,
            "report_path": report_path,
            "trace_path": OUTPUT_DIR / f"trace_{trace_id}.json",
            "metrics_path": OUTPUT_DIR / f"metrics_{trace_id}.json",
            "audit_path": OUTPUT_DIR / f"audit_{run_ts}.jsonl",
        })
    runs.sort(key=lambda r: r["generated_at"], reverse=True)
    return runs


def latest_eval_report() -> Optional[Dict[str, Any]]:
    """最新评测报告（文件名含时间戳，倒序取第一个可解析的）。"""
    for path in sorted(OUTPUT_DIR.glob("eval_report_*.json"), reverse=True):
        data = load_json(path)
        if data:
            data["_path"] = str(path)
            return data
    return None


def get_golden_dataset() -> List[Dict[str, Any]]:
    """加载 Golden Dataset。"""
    return load_jsonl(GOLDEN_PATH)


def get_cost_trend() -> List[Dict[str, Any]]:
    """跨运行成本趋势聚合。"""
    try:
        from opspilot.evaluation.cost import aggregate_cost_trend
        return aggregate_cost_trend(OUTPUT_DIR)
    except ImportError:
        # 备用实现：直接读文件
        trend = []
        for path in sorted(OUTPUT_DIR.glob("metrics_*.json")):
            data = load_json(path)
            if not data:
                continue
            cost = data.get("cost") or {}
            trend.append({
                "trace_id": data.get("trace_id", ""),
                "generated_at": data.get("generated_at", ""),
                "file": path.name,
                "total_cost": cost.get("total_cost", 0.0),
                "llm_call_count": cost.get("llm_call_count", 0),
                "budget_exceeded": (cost.get("budget") or {}).get("exceeded", False),
                "per_agent": cost.get("per_agent", {}),
                "per_model": cost.get("per_model", {}),
            })
        trend.sort(key=lambda item: item.get("generated_at") or item["file"])
        return trend
