# -*- coding: utf-8 -*-
"""多方案协商决策：对多个（根因候选, 修复方案）按 风险 × 置信度 × 预估恢复时长 打分排序。

用于协商模式（机制 2）：RcaAgent 产出多个置信度接近的根因候选时，
PlannerAgent 为每个候选并行生成独立方案，本模块给出确定性的量化排序，
交互模式下供人工从多方案中选择，--auto-approve 下自动选最优。
"""
from __future__ import annotations

from typing import Any, Dict, List

# 风险等级 -> 得分因子（风险越高越保守，得分越低）
_RISK_FACTOR = {"low": 1.0, "medium": 0.8, "high": 0.55}

# 动作类型 -> 预估耗时（分钟），未知类型按保守值估算
_ACTION_MINUTES = {
    "rollback_change": 15,
    "scale_pool": 5,
    "config_update": 5,
    "rolling_restart": 10,
    "diagnostic_capture": 10,
    "traffic_switch": 8,
    "freeze_account": 10,
    "trigger_2fa": 8,
    "notify_team": 5,
    "manual_followup": 30,
}
_DEFAULT_ACTION_MINUTES = 15


def estimate_recovery_minutes(plan: Dict[str, Any]) -> int:
    """按方案步骤的动作类型累加预估恢复时长（分钟）。"""
    return sum(
        _ACTION_MINUTES.get(step.get("action_type", ""), _DEFAULT_ACTION_MINUTES)
        for step in plan.get("steps", [])
    )


def score_plan_option(candidate: Dict[str, Any], plan: Dict[str, Any]) -> Dict[str, Any]:
    """单个（候选, 方案）打分：score = 置信度 × 风险因子 / (1 + 时长/60)。

    置信度高、风险低、恢复快的方案得分更高；公式确定性可复现，
    打分细节全部随选项返回（评审可见的决策依据）。
    """
    minutes = estimate_recovery_minutes(plan)
    risk_level = plan.get("risk_level", "high")
    risk_factor = _RISK_FACTOR.get(risk_level, 0.55)
    confidence = float(candidate.get("confidence", 0.0))
    score = round(confidence * risk_factor / (1 + minutes / 60), 4)
    return {
        "candidate": candidate,
        "plan": plan,
        "score": score,
        "score_breakdown": {
            "confidence": confidence,
            "risk_level": risk_level,
            "risk_factor": risk_factor,
            "estimated_recovery_minutes": minutes,
            "formula": "score = confidence * risk_factor / (1 + minutes / 60)",
        },
    }


def rank_plan_options(
    pairs: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """对多个 {candidate, plan} 打分并降序排序（同分按置信度，再按原顺序稳定）。"""
    options = [score_plan_option(p["candidate"], p["plan"]) for p in pairs]
    options.sort(
        key=lambda o: (o["score"], o["score_breakdown"]["confidence"]), reverse=True,
    )
    for i, option in enumerate(options):
        option["rank"] = i + 1
    return options
