# -*- coding: utf-8 -*-
"""RcaAgent：根因分析 Agent。

职责：调用 LogTraceRca 做日志+链路+指标+变更的多维关联分析，
选出最高置信度的根因候选；再经 CaseRetrieval 检索相似历史案例
注入上下文增强判断，最后用 LLM 生成可读的分析结论。

协商模式（content 携带 negotiation.enabled 时）额外具备反馈协商能力：
- 低置信度反馈环：Top1 置信度低于阈值时不直接降级，而是向 Orchestrator
  发起证据补充请求（needs_more_evidence + 缺失数据类型清单），获得补充
  证据后进行第二轮分析（重试上限 max_evidence_rounds，防死循环）；
- 多假设产出：变更强相关时同时产出竞争性假设候选，供多方案协商决策。
默认模式（无 negotiation 配置）行为与原实现完全一致。
"""
from __future__ import annotations

import json
from typing import Any, Dict, List

from .base import BaseAgent

# 证据维度 -> 补充采集请求类型（weak/missing 维度触发对应请求）
_EVIDENCE_REQUEST_TYPES = {
    "logs": "extended_time_window_logs",
    "metrics": "extended_metrics",
    "traces": "full_trace_dump",
    "changes": "change_ticket_details",
}


class RcaAgent(BaseAgent):
    config_key = "rca_agent"

    def output_message_type(self) -> str:
        return "root_cause"

    def process(self, content: Dict[str, Any]) -> Dict[str, Any]:
        incident = content["incident"]
        negotiation = content.get("negotiation") or {}
        neg_enabled = bool(negotiation.get("enabled"))
        threshold = float(negotiation.get("rca_confidence_threshold", 0.6))
        max_rounds = int(negotiation.get("max_evidence_rounds", 1))
        evidence_round = int(content.get("evidence_round", 0))
        supplemental = content.get("supplemental_evidence") or {}

        # 1. 多维根因分析（失败策略 abort，由 Orchestrator 统一降级）
        rca_payload: Dict[str, Any] = {
            "affected_services": incident["affected_services"],
            "first_alert_at": content["first_alert_at"],
        }
        if neg_enabled:
            rca_payload["multi_hypothesis"] = True
        if supplemental:
            # 第二轮：补充证据注入，分析过程记录到 rca.reanalysis span
            rca_payload["supplemental"] = supplemental
            with self.tracer.start_span(
                "rca.reanalysis",
                kind="INTERNAL",
                attributes={"rca.evidence_round": evidence_round,
                            "rca.supplemental_log_count": len(supplemental.get("extended_logs") or []),
                            "rca.supplemental_change_count": len(supplemental.get("change_details") or [])},
            ):
                self.logger.info(
                    f"  ↻ 第 {evidence_round + 1} 轮根因分析（已注入补充证据: "
                    f"扩展日志 {len(supplemental.get('extended_logs') or [])} 条，"
                    f"变更单详情 {len(supplemental.get('change_details') or [])} 条）"
                )
                rca = self.call_skill("log_trace_rca", rca_payload)
        else:
            rca = self.call_skill("log_trace_rca", rca_payload)
        candidates = rca.output["candidates"]

        # 2. 选取最高置信度候选（列表已按置信度降序）
        selected = candidates[0] if candidates else None

        # 2.5 协商模式：低置信度反馈环（evidence request loop）
        if neg_enabled and selected and selected["confidence"] < threshold:
            if evidence_round < max_rounds:
                return self._request_more_evidence(
                    content, candidates, selected, threshold, evidence_round,
                )
            # 重试轮次耗尽仍低于阈值：走原有降级路径（转人工），如实标注
            self.logger.warning(
                f"  ⚠ 第 {evidence_round + 1} 轮分析后置信度 {selected['confidence']} 仍低于阈值 "
                f"{threshold}，重试上限已到，按原降级路径转人工"
            )
            if self.skill_context.audit:
                self.skill_context.audit.record(
                    "rca_low_confidence_handoff",
                    incident_id=incident.get("incident_id"),
                    confidence=selected["confidence"], threshold=threshold,
                    evidence_rounds_used=evidence_round,
                )
            return {
                **self._strip_negotiation_keys(content),
                "root_cause_candidates": candidates,
                "selected_root_cause": None,
                "similar_cases": [],
                "analysis": "",
                "low_confidence_handoff": True,
            }

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
            **self._strip_negotiation_keys(content),
            "root_cause_candidates": candidates,
            "selected_root_cause": selected,
            "similar_cases": similar_cases,
            "analysis": analysis,
        }

    def _request_more_evidence(
        self,
        content: Dict[str, Any],
        candidates: List[Dict[str, Any]],
        selected: Dict[str, Any],
        threshold: float,
        evidence_round: int,
    ) -> Dict[str, Any]:
        """发起证据补充请求：返回 needs_more_evidence 信号 + 缺失数据类型清单。"""
        # 缺失清单：扩展时间窗日志为基础请求项，weak/missing 证据维度追加对应类型
        missing = ["extended_time_window_logs"]
        for ev in selected.get("evidences", []):
            req = _EVIDENCE_REQUEST_TYPES.get(ev.get("source"))
            if ev.get("strength") in ("weak", "missing") and req and req not in missing:
                missing.append(req)
        with self.tracer.start_span(
            "rca.evidence_request",
            kind="INTERNAL",
            attributes={"rca.confidence": selected["confidence"],
                        "rca.threshold": threshold,
                        "rca.evidence_round": evidence_round,
                        "rca.missing_evidence": ", ".join(missing)},
        ):
            self.logger.warning(
                f"  ✋ Top1 置信度 {selected['confidence']} 低于阈值 {threshold}，"
                f"向 Orchestrator 发起证据补充请求: {', '.join(missing)}"
            )
            if self.skill_context.audit:
                self.skill_context.audit.record(
                    "evidence_request",
                    incident_id=content["incident"].get("incident_id"),
                    confidence=selected["confidence"], threshold=threshold,
                    evidence_round=evidence_round, missing_evidence=missing,
                )
        return {
            **content,
            "root_cause_candidates": candidates,
            "selected_root_cause": None,
            "similar_cases": [],
            "analysis": "",
            "needs_more_evidence": True,
            "missing_evidence": missing,
            "evidence_round": evidence_round + 1,
        }

    @staticmethod
    def _strip_negotiation_keys(content: Dict[str, Any]) -> Dict[str, Any]:
        """出站上下文清理反馈环过程键，避免污染下游阶段与最终报告。"""
        return {
            k: v for k, v in content.items()
            if k not in ("negotiation", "needs_more_evidence", "missing_evidence",
                         "supplemental_evidence", "evidence_round")
        }
