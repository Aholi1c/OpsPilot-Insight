# -*- coding: utf-8 -*-
"""VerifierAgent：恢复验证与复盘 Agent。

职责：
1. RecoveryVerify 对比故障期与修复后指标，判定告警消除与指标回归基线；
2. Postmortem 生成复盘报告（时间线/根因/处置/效果/改进建议）；
3. 将本次事故沉淀为历史案例写入知识库（同 incident_id 幂等），
   供后续 CaseRetrieval 命中，形成经验闭环。
"""
from __future__ import annotations

import json
from typing import Any, Dict, Optional

from ..models import PostmortemReport, VerificationResult
from .base import BaseAgent


class VerifierAgent(BaseAgent):
    config_key = "verifier_agent"

    def output_message_type(self) -> str:
        return "verification_report"

    def process(self, content: Dict[str, Any]) -> Dict[str, Any]:
        incident: Dict[str, Any] = content["incident"]

        # ---- 1. 恢复验证（degrade：验证失败时报告标记未验证，不阻断复盘）----
        verify_result = self.call_skill("recovery_verify", {"incident": incident})
        verification: Optional[Dict[str, Any]] = (
            verify_result.output if verify_result.success else None
        )
        verification_model = VerificationResult(
            **verification
        ) if verification else VerificationResult(
            passed=False, alerts_cleared=False,
            summary=f"恢复验证阶段失败（{verify_result.error}），效果待人工确认",
        )

        # ---- 2. 复盘报告生成 ----
        pm_result = self.call_skill("postmortem", {
            "incident": incident,
            "root_cause": content.get("selected_root_cause"),
            "plan": content.get("remediation_plan"),
            "execution_result": content.get("execution_result"),
            "verification": verification,
            "pipeline_timeline": content.get("pipeline_timeline", []),
        })
        if not pm_result.success:
            self.logger.warning(f"复盘生成失败（{pm_result.error}），报告不含复盘段落")
            return {**content, "verification_result": verification_model.model_dump(),
                    "postmortem": None}
        pm = pm_result.output

        # LLM 生成复盘叙述（上下文注入根因/效果/改进项）
        narrative = self.call_llm(json.dumps(
            {"incident_title": incident.get("title", ""), "root_cause": pm["root_cause"],
             "effect": pm["effect"], "improvements": pm["improvements"]},
            ensure_ascii=False,
        ))

        # ---- 3. 案例沉淀（幂等：同 incident_id 不重复写入）----
        case_id = self._persist_case(pm["case"])

        postmortem = PostmortemReport(
            incident_id=incident.get("incident_id", ""),
            title=incident.get("title", ""),
            timeline=pm["timeline"],
            root_cause=pm["root_cause"],
            actions_taken=pm["actions_taken"],
            effect=pm["effect"],
            improvements=pm["improvements"],
            narrative=narrative,
            case_id=case_id,
        )
        self.logger.info(
            f"  ✦ 验证与复盘完成: 验证{'通过' if verification_model.passed else '未通过'}，"
            f"案例沉淀={'已写入 ' + case_id if case_id else '跳过（已存在或知识库不可用）'}"
        )
        return {
            **content,
            "verification_result": verification_model.model_dump(),
            "postmortem": postmortem.model_dump(),
        }

    def _persist_case(self, case: Dict[str, Any]) -> str:
        """事故案例写入知识库（幂等），返回案例 ID（未写入返回空串）。"""
        store = self.skill_context.extras.get("knowledge_store")
        if store is None:
            self.logger.warning("知识库未装配，跳过案例沉淀")
            return ""
        written = store.append_case(case)
        if not written:
            self.logger.info(f"    ↷ 案例已存在（incident_id={case.get('incident_id')}），幂等跳过")
            return ""
        return case.get("id", "")
