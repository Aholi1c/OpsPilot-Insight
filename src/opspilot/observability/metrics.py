# -*- coding: utf-8 -*-
"""进程内指标收集器：LLM 调用 / Skill 成功率 / Agent 耗时 / pipeline 总耗时。

- 一次流水线运行 = 一个 MetricsCollector 实例，与 trace_id 关联；
- 运行结束由 Orchestrator 导出 output/metrics_*.json；
- token 数为估算值（区分 CJK 与 ASCII 字符分别估算，仅用于成本感知，非计费口径）；
- 逐次 LLM 调用事件按 agent/model/skill 归因，并汇总为 cost 成本段。
"""
from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Union


def _estimate_tokens(chars: int) -> int:
    """按字符数粗估 token 数（中文约 1.5 字符/token，无原文时的兜底口径）。"""
    return int(math.ceil(chars / 1.5))


def estimate_tokens(text: str) -> int:
    """细化 token 估算：CJK 约 1.5 字符/token，ASCII/其它约 4 字符/token。

    MockProvider 路径使用该估算；DashScope 路径优先读取 API 返回的真实 usage。
    """
    if not text:
        return 0
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    other = len(text) - cjk
    return int(math.ceil(cjk / 1.5 + other / 4.0))


class MetricsCollector:
    """轻量进程内指标收集（无第三方依赖）。"""

    def __init__(self, trace_id: str = ""):
        self.trace_id = trace_id
        self.llm: Dict[str, Any] = {
            "call_count": 0, "prompt_chars": 0, "completion_chars": 0,
            "estimated_prompt_tokens": 0, "estimated_completion_tokens": 0,
        }
        self.llm_calls: List[Dict[str, Any]] = []     # 逐次调用事件（成本三维归因依据）
        self.skills: Dict[str, Dict[str, Any]] = {}   # skill 名 -> 调用数/成功数/耗时
        self.agents: Dict[str, Dict[str, Any]] = {}   # agent 名 -> 调用数/耗时
        self.rag: Dict[str, Any] = {"query_count": 0, "hit_count": 0}
        self.pipeline_duration_ms: float = 0.0
        self.cost: Optional[Dict[str, Any]] = None    # 由 evaluation.cost 在运行结束时注入

    # ---- 记录接口 ----
    def record_llm_call(
        self,
        provider: str,
        prompt_chars: int,
        completion_chars: int,
        agent: str = "",
        model: str = "",
        skill: str = "",
        prompt_tokens: Optional[int] = None,
        completion_tokens: Optional[int] = None,
        token_source: str = "estimated",
    ) -> None:
        """记录一次 LLM 调用；token 未传入时按字符数兜底估算。"""
        p_tokens = prompt_tokens if prompt_tokens is not None else _estimate_tokens(prompt_chars)
        c_tokens = (completion_tokens if completion_tokens is not None
                    else _estimate_tokens(completion_chars))
        self.llm["call_count"] += 1
        self.llm["provider"] = provider
        self.llm["prompt_chars"] += prompt_chars
        self.llm["completion_chars"] += completion_chars
        self.llm["estimated_prompt_tokens"] += p_tokens
        self.llm["estimated_completion_tokens"] += c_tokens
        self.llm_calls.append({
            "provider": provider,
            "model": model or provider,
            "agent": agent,
            "skill": skill,
            "prompt_tokens": p_tokens,
            "completion_tokens": c_tokens,
            "token_source": token_source,
        })

    def record_skill(self, name: str, success: bool, duration_ms: float) -> None:
        entry = self.skills.setdefault(name, {"call_count": 0, "success_count": 0, "total_ms": 0.0})
        entry["call_count"] += 1
        entry["success_count"] += 1 if success else 0
        entry["total_ms"] = round(entry["total_ms"] + duration_ms, 3)

    def record_agent(self, name: str, duration_ms: float) -> None:
        entry = self.agents.setdefault(name, {"call_count": 0, "total_ms": 0.0})
        entry["call_count"] += 1
        entry["total_ms"] = round(entry["total_ms"] + duration_ms, 3)

    def record_rag_query(self, hit_count: int) -> None:
        self.rag["query_count"] += 1
        self.rag["hit_count"] += hit_count

    def record_pipeline(self, duration_ms: float) -> None:
        self.pipeline_duration_ms = round(duration_ms, 3)

    def set_cost(self, cost_section: Dict[str, Any]) -> None:
        """注入成本分解段（由 evaluation.cost.compute_cost_section 计算）。"""
        self.cost = cost_section

    # ---- 导出 ----
    def export(self) -> Dict[str, Any]:
        skills = {
            name: {
                **entry,
                "success_rate": round(entry["success_count"] / entry["call_count"], 4)
                if entry["call_count"] else 0.0,
            }
            for name, entry in self.skills.items()
        }
        data: Dict[str, Any] = {
            "trace_id": self.trace_id,
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "pipeline_duration_ms": self.pipeline_duration_ms,
            "llm": {**self.llm, "calls": list(self.llm_calls)},
            "skills": skills,
            "agents": dict(self.agents),
            "rag": dict(self.rag),
        }
        if self.cost is not None:
            data["cost"] = self.cost
        return data

    def export_json(self, path: Union[str, Path]) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.export(), ensure_ascii=False, indent=2), encoding="utf-8")
        return path
