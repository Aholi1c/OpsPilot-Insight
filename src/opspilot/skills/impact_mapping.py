# -*- coding: utf-8 -*-
"""ImpactMapping Skill：影响面评估。

基于链路数据推导服务依赖拓扑，从告警涉及服务出发沿"调用方"方向反向 BFS，
得到受影响的上游服务集合（爆炸半径），并评估是否波及用户侧入口。
"""
from __future__ import annotations

from typing import Any, Dict, List, Set

from .base import Skill, SkillContext

# 视为用户侧入口的服务（命中则判定用户可感知）
_ENTRY_SERVICES = {"api-gateway", "web-gateway", "mobile-gateway"}


class ImpactMappingSkill(Skill):
    name = "impact_mapping"
    version = "1.0.0"
    description = "影响面评估：依赖拓扑反向传播，计算爆炸半径与用户影响"
    input_schema = {"services": "告警直接涉及的服务列表"}
    output_schema = {
        "affected_services": "直接受影响服务",
        "upstream_impacted": "受波及的上游调用方",
        "downstream_dependencies": "下游依赖（辅助定位根因方向）",
        "blast_radius": "爆炸半径内全部服务",
        "user_facing": "是否波及用户侧入口",
        "user_impact": "用户影响描述",
    }
    preconditions = ["services"]
    failure_policy = "degrade"  # 影响面缺失不阻断根因分析，可降级

    def run(self, payload: Dict[str, Any], context: SkillContext) -> Dict[str, Any]:
        affected: List[str] = list(payload["services"])
        tracing = context.adapters.get("tracing")
        edges = tracing.get_service_dependencies() if tracing else []

        # 反向邻接表：callee -> [caller]，用于故障向上游传播
        callers: Dict[str, List[str]] = {}
        callees: Dict[str, List[str]] = {}
        for edge in edges:
            callers.setdefault(edge["callee"], []).append(edge["caller"])
            callees.setdefault(edge["caller"], []).append(edge["callee"])

        # 从受影响服务出发反向 BFS，找到全部受波及的上游
        impacted: Set[str] = set(affected)
        queue = list(affected)
        while queue:
            current = queue.pop(0)
            for caller in callers.get(current, []):
                if caller not in impacted:
                    impacted.add(caller)
                    queue.append(caller)
        upstream = sorted(impacted - set(affected))

        # 下游依赖：辅助 RCA 判断故障方向（如 DB / 依赖服务）
        downstream = sorted({d for svc in affected for d in callees.get(svc, [])} - set(affected))

        user_facing = bool(impacted & _ENTRY_SERVICES)
        user_impact = (
            f"故障已沿调用链传播至入口服务 {sorted(impacted & _ENTRY_SERVICES)}，用户侧可感知"
            if user_facing else "尚未波及用户侧入口服务，属内部影响"
        )
        context.logger.info(
            f"    影响面: 直接受影响 {affected}，波及上游 {upstream}，用户可感知={user_facing}",
        )
        return {
            "affected_services": affected,
            "upstream_impacted": upstream,
            "downstream_dependencies": downstream,
            "blast_radius": sorted(impacted),
            "blast_radius_size": len(impacted),
            "user_facing": user_facing,
            "user_impact": user_impact,
        }
