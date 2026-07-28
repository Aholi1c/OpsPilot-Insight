# -*- coding: utf-8 -*-
"""AlertAgent：告警接入 Agent。

职责：调用 AlertFusion（去重聚合）+ ImpactMapping（影响面评估），
将原始告警流收敛为结构化 Incident，并用 LLM 生成一句话事件摘要。
"""
from __future__ import annotations

import json
import uuid
from typing import Any, Dict

from ..models import Incident
from .base import BaseAgent


class AlertAgent(BaseAgent):
    config_key = "alert_agent"

    def output_message_type(self) -> str:
        return "incident"

    def process(self, content: Dict[str, Any]) -> Dict[str, Any]:
        # 1. 告警去重聚合（失败策略为 abort：无有效告警则流水线终止）
        fusion = self.call_skill("alert_fusion", {"alerts": content["alerts"]})
        fusion_out = fusion.output

        # 2. 影响面评估（可降级：失败时用告警涉及服务兜底）
        impact = self.call_skill("impact_mapping", {"services": fusion_out["services"]})
        if impact.success:
            impact_out = impact.output
        else:
            self.logger.warning("影响面评估失败，降级为仅使用告警涉及服务", error=impact.error)
            impact_out = {
                "affected_services": fusion_out["services"],
                "blast_radius": fusion_out["services"],
                "user_facing": False,
                "user_impact": "影响面评估降级，范围未知",
            }

        # 3. LLM 生成一句话事件摘要（上下文注入告警组与影响面）
        summary = self.call_llm(json.dumps(
            {"alert_groups": fusion_out["groups"], "impact": impact_out},
            ensure_ascii=False,
        ))
        self.logger.info(f"  ✦ 事件摘要: {summary}")

        # 4. 收敛为 Incident 对象
        incident = Incident(
            incident_id=f"INC-{uuid.uuid4().hex[:8].upper()}",
            title=f"{fusion_out['groups'][0]['alertname']} @ {fusion_out['groups'][0]['service']}",
            severity=fusion_out["severity"],
            raw_alert_count=fusion_out["raw_count"],
            deduped_alert_count=fusion_out["deduped_count"],
            alert_groups=fusion_out["groups"],
            affected_services=impact_out["affected_services"],
            impact=impact_out,
            summary=summary,
        )
        return {
            "incident": incident.model_dump(),
            "first_alert_at": fusion_out["first_alert_at"],
        }
