# -*- coding: utf-8 -*-
"""成本追踪：LLM 成本三维分解（per-Agent / per-Skill / per-Model）+ 预算控制 + 趋势聚合。

成本口径：
- 单价来自 config/pricing.yaml（元 / 1000 token，Mock 模型使用虚拟单价便于演示）；
- token 数来自 metrics 的逐次 LLM 调用事件（Mock 按字符估算 / DashScope 读真实 usage）；
- per-Agent / per-Model：按调用事件的 agent / model 字段直接聚合；
- per-Skill：LLM 调用若由 Skill span 链发起则直接归因到该 Skill；Agent 直接推理的
  成本按该 Agent 本次调用的各 Skill 耗时占比近似分摊（Skill 是 Agent 决策链的组成
  部分），无 Skill 调用的 Agent 计入 "<Agent>:direct" 桶——三个维度求和均等于总成本。

预算控制：pricing.yaml 的 budget.per_incident 为单次事故处理预算上限；
超限不中断流程，仅置 exceeded=True（Orchestrator 据此记 audit 事件 budget_alert）。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from ..config import load_yaml

# 未在 pricing.yaml 中配置的模型使用 default 单价兜底
_FALLBACK_PRICE = {"input_per_1k": 0.008, "output_per_1k": 0.02}


def load_pricing(path: Union[str, Path]) -> Dict[str, Any]:
    """加载成本模型配置；文件缺失时返回内置兜底（保证核心流程不因缺配置中断）。"""
    path = Path(path)
    if not path.exists():
        return {"currency": "CNY", "models": {"default": dict(_FALLBACK_PRICE)},
                "budget": {"per_incident": 0.0}}
    data = load_yaml(path)
    data.setdefault("currency", "CNY")
    data.setdefault("models", {})
    data.setdefault("budget", {})
    return data


def _model_price(pricing: Dict[str, Any], model: str) -> Dict[str, float]:
    models = pricing.get("models") or {}
    return models.get(model) or models.get("default") or dict(_FALLBACK_PRICE)


def estimate_call_cost(event: Dict[str, Any], pricing: Dict[str, Any]) -> float:
    """按单次 LLM 调用事件（prompt/completion token）估算成本（元）。"""
    price = _model_price(pricing, event.get("model", ""))
    return (event.get("prompt_tokens", 0) / 1000.0 * float(price.get("input_per_1k", 0))
            + event.get("completion_tokens", 0) / 1000.0 * float(price.get("output_per_1k", 0)))


def _skill_durations_by_agent(trace: Optional[Dict[str, Any]]) -> Dict[str, Dict[str, float]]:
    """从 trace span 树提取：agent 名 -> {skill 名: 累计耗时 ms}（per-Skill 分摊权重）。"""
    result: Dict[str, Dict[str, float]] = {}
    if not trace:
        return result
    spans = trace.get("spans", [])
    by_id = {s["span_id"]: s for s in spans}
    for span in spans:
        if not span["name"].startswith("skill."):
            continue
        # 沿父链找到所属 agent span
        parent_id, agent = span.get("parent_span_id"), ""
        while parent_id:
            parent = by_id.get(parent_id)
            if parent is None:
                break
            if parent["name"].startswith("agent."):
                agent = parent["name"][len("agent."):]
                break
            parent_id = parent.get("parent_span_id")
        if not agent:
            continue
        skill = span["name"][len("skill."):]
        bucket = result.setdefault(agent, {})
        bucket[skill] = bucket.get(skill, 0.0) + (span.get("duration_ms") or 0.0)
    return result


def compute_cost_section(
    metrics: Dict[str, Any],
    trace: Optional[Dict[str, Any]],
    pricing: Dict[str, Any],
) -> Dict[str, Any]:
    """计算 metrics_*.json 的 cost 段：三维分解 + 预算状态。"""
    events: List[Dict[str, Any]] = (metrics.get("llm") or {}).get("calls", [])
    per_agent: Dict[str, float] = {}
    per_model: Dict[str, float] = {}
    per_skill: Dict[str, float] = {}
    agent_direct: Dict[str, float] = {}  # 未经 Skill span 发起的调用，待按 Skill 耗时分摊
    total = 0.0

    for event in events:
        cost = estimate_call_cost(event, pricing)
        total += cost
        agent = event.get("agent") or "(unknown)"
        model = event.get("model") or "(unknown)"
        per_agent[agent] = per_agent.get(agent, 0.0) + cost
        per_model[model] = per_model.get(model, 0.0) + cost
        skill = event.get("skill") or ""
        if skill:  # 由 Skill span 链直接发起的 LLM 调用，精确归因
            per_skill[skill] = per_skill.get(skill, 0.0) + cost
        else:
            agent_direct[agent] = agent_direct.get(agent, 0.0) + cost

    # Agent 直接推理成本按其调用的 Skill 耗时占比分摊（近似口径，保证三维求和一致）
    skill_ms = _skill_durations_by_agent(trace)
    for agent, cost in agent_direct.items():
        weights = skill_ms.get(agent) or {}
        total_ms = sum(weights.values())
        if total_ms <= 0:
            key = f"{agent}:direct"
            per_skill[key] = per_skill.get(key, 0.0) + cost
            continue
        for skill, ms in weights.items():
            per_skill[skill] = per_skill.get(skill, 0.0) + cost * ms / total_ms

    budget_limit = float((pricing.get("budget") or {}).get("per_incident") or 0.0)
    exceeded = budget_limit > 0 and total > budget_limit
    round6 = lambda d: {k: round(v, 6) for k, v in sorted(d.items(), key=lambda x: -x[1])}  # noqa: E731
    return {
        "currency": pricing.get("currency", "CNY"),
        "total_cost": round(total, 6),
        "llm_call_count": len(events),
        "per_agent": round6(per_agent),
        "per_model": round6(per_model),
        "per_skill": round6(per_skill),
        "budget": {
            "limit": budget_limit,
            "exceeded": exceeded,
            "usage_ratio": round(total / budget_limit, 4) if budget_limit > 0 else None,
        },
        "attribution_note": (
            "per-Skill 为近似口径：Skill 链直接发起的 LLM 调用精确归因，"
            "Agent 直接推理成本按该 Agent 各 Skill 耗时占比分摊"
        ),
    }


def aggregate_cost_trend(output_dir: Union[str, Path]) -> List[Dict[str, Any]]:
    """跨多次运行的成本趋势：读取 output/ 下所有 metrics_*.json，按时间排序。"""
    output_dir = Path(output_dir)
    trend: List[Dict[str, Any]] = []
    for path in sorted(output_dir.glob("metrics_*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        cost = data.get("cost") or {}
        trend.append({
            "trace_id": data.get("trace_id", ""),
            "generated_at": data.get("generated_at", ""),
            "file": path.name,
            "total_cost": cost.get("total_cost", 0.0),
            "llm_call_count": cost.get("llm_call_count",
                                       (data.get("llm") or {}).get("call_count", 0)),
            "budget_exceeded": (cost.get("budget") or {}).get("exceeded", False),
            "per_agent": cost.get("per_agent", {}),
            "per_model": cost.get("per_model", {}),
        })
    trend.sort(key=lambda item: item.get("generated_at") or item["file"])
    return trend
