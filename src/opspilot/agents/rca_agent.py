# -*- coding: utf-8 -*-
"""RcaAgent：根因分析 Agent。

职责：调用 LogTraceRca 做日志+链路+指标+变更的多维关联分析，
选出最高置信度的根因候选；再经 CaseRetrieval 检索相似历史案例
注入上下文增强判断，最后用 LLM 生成可读的分析结论。
"""
from __future__ import annotations

import json
from typing import Any, Dict

from .base import BaseAgent


class RcaAgent(BaseAgent):
    config_key = "rca_agent"

    def output_message_type(self) -> str:
        return "root_cause"

    def process(self, content: Dict[str, Any]) -> Dict[str, Any]:
        incident = content["incident"]

        # 1. 多维根因分析（失败策略 abort，由 Orchestrator 统一降级）
        rca = self.call_skill("log_trace_rca", {
            "affected_services": incident["affected_services"],
            "first_alert_at": content["first_alert_at"],
        })
        candidates = rca.output["candidates"]

        # 2. 选取最高置信度候选（列表已按置信度降序）
        selected = candidates[0] if candidates else None
        if selected:
            self.logger.info(
                f"  ✦ 选定根因（置信度 {selected['confidence']}）: {selected['hypothesis']}"
            )

        # 3. 检索相似历史案例（RAG 增强项，degrade：失败时无案例注入）
        similar_cases = []
        case_result = self.call_skill("case_retrieval", {
            "query": incident["title"] + " " + incident.get("summary", "")
            + (" " + selected["hypothesis"] if selected else ""),
        })
        if case_result.success:
            similar_cases = case_result.output["cases"]

        # 4. LLM 生成分析结论（上下文注入候选、证据链与相似历史案例）
        analysis = self.call_llm(json.dumps(
            {"incident_title": incident["title"], "candidates": candidates,
             "similar_historical_cases": [
                 {"title": c["title"], "root_cause": c["root_cause"],
                  "resolution": c["resolution"]} for c in similar_cases
             ]},
            ensure_ascii=False,
        ))
        self.logger.info(f"  ✦ 分析结论: {analysis}")

        return {
            "incident": incident,
            "root_cause_candidates": candidates,
            "selected_root_cause": selected,
            "similar_cases": similar_cases,
            "analysis": analysis,
        }
