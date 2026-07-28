# -*- coding: utf-8 -*-
"""RiskGuard Skill：执行前风险评估（ExecutorAgent 用）。

评估维度：
- 动作风险等级：逐动作查白名单 default_risk，与方案自身 risk_level 取最高；
- 影响半径：事件涉及的服务数与服务清单（半径越大越需要谨慎）；
- 审批判定：综合风险为 medium/high 时需要人工审批后才可执行。

同时标记不在白名单内的动作类型，供 SafeExecute 拒绝时提前预警。
"""
from __future__ import annotations

from typing import Any, Dict, List

from .base import Skill, SkillContext

# 风险等级排序（用于取最高）
_RISK_ORDER = {"low": 0, "medium": 1, "high": 2}


class RiskGuardSkill(Skill):
    name = "risk_guard"
    version = "1.0.0"
    description = "执行前风险评估：动作风险等级 + 影响半径 + 是否需要人工审批"
    input_schema = {
        "plan": "RemediationPlan 序列化字典",
        "incident": "Incident 序列化字典（用于评估影响半径）",
    }
    output_schema = {
        "overall_risk": "综合风险等级 low/medium/high",
        "needs_approval": "是否需要人工审批",
        "action_risks": "逐动作风险评估",
        "impact_radius": "影响半径（受影响服务清单与数量）",
        "unknown_action_types": "不在白名单内的动作类型（执行时将被拒绝）",
    }
    preconditions = ["plan", "incident"]
    failure_policy = "degrade"  # 评估失败时由 ExecutorAgent 按最保守策略处理

    def run(self, payload: Dict[str, Any], context: SkillContext) -> Dict[str, Any]:
        plan: Dict[str, Any] = payload["plan"]
        incident: Dict[str, Any] = payload["incident"]
        whitelist: Dict[str, Any] = context.extras.get("action_whitelist", {})

        # 1. 逐动作风险：白名单 default_risk；未知类型按 high 计并单独标记
        action_risks: List[Dict[str, Any]] = []
        unknown_types: List[str] = []
        max_risk = plan.get("risk_level", "low")
        for step in plan.get("steps", []):
            action_type = step.get("action_type", "")
            entry = whitelist.get(action_type)
            if entry is None:
                risk = "high"
                if action_type not in unknown_types:
                    unknown_types.append(action_type)
            else:
                risk = entry.get("default_risk", "medium")
            action_risks.append({
                "order": step.get("order", 0),
                "action_type": action_type,
                "risk": risk,
                "whitelisted": entry is not None,
            })
            if _RISK_ORDER.get(risk, 2) > _RISK_ORDER.get(max_risk, 0):
                max_risk = risk

        # 2. 影响半径：事件涉及的服务范围
        services = incident.get("affected_services", []) or []
        impact_radius = {
            "services": services,
            "service_count": len(services),
            "severity": incident.get("severity", "unknown"),
        }

        # 3. 审批判定：medium 及以上必须人工审批（与 Planner 决策边界一致）
        needs_approval = _RISK_ORDER.get(max_risk, 2) >= _RISK_ORDER["medium"]
        context.logger.info(
            f"    ⚖ 风险评估: 综合风险={max_risk}，影响服务 {len(services)} 个，"
            f"需审批={needs_approval}"
            + (f"，未知动作类型 {unknown_types}" if unknown_types else "")
        )
        return {
            "overall_risk": max_risk,
            "needs_approval": needs_approval,
            "action_risks": action_risks,
            "impact_radius": impact_radius,
            "unknown_action_types": unknown_types,
        }
