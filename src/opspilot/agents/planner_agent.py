# -*- coding: utf-8 -*-
"""PlannerAgent：修复规划 Agent。

职责：基于选定根因生成修复方案（分步动作 + 风险等级 + 回滚计划），
方案引用 RunbookRag 检索到的标准预案作为依据，并结合 CaseRetrieval
命中的历史案例处置经验，用 LLM 生成方案说明。本 Agent 只产出方案，
不执行任何变更（安全执行由 ExecutorAgent 负责）。
"""
from __future__ import annotations

import json
import uuid
from typing import Any, Dict, List, Optional

from ..models import RemediationPlan, RemediationStep
from .base import BaseAgent

# 风险等级 -> 是否需要审批（决策边界：medium 及以上必须人工审批后才可执行）
_APPROVAL_POLICY = {"low": False, "medium": True, "high": True}


def _steps(raw: List[Dict[str, Any]]) -> List[RemediationStep]:
    """按顺序号构建步骤列表。"""
    return [RemediationStep(order=i + 1, **item) for i, item in enumerate(raw)]


class PlannerAgent(BaseAgent):
    config_key = "planner_agent"

    def output_message_type(self) -> str:
        return "remediation_plan"

    def process(self, content: Dict[str, Any]) -> Dict[str, Any]:
        selected: Optional[Dict[str, Any]] = content.get("selected_root_cause")
        if not selected:
            # 无可信根因时不生成自动方案，转人工（决策边界内的保守行为）
            self.logger.warning("无可信根因候选，跳过自动方案生成，建议转人工处理")
            return {**content, "remediation_plan": None}

        incident = content["incident"]
        # 修复动作面向根因的主嫌疑服务（而非告警涉及的第一个服务）
        service = selected.get("service") or (
            incident["affected_services"][0] if incident["affected_services"] else "unknown"
        )
        plan = self._build_plan(selected, service)

        # RunbookRag 检索标准预案作为方案依据（degrade：失败时方案不带引用）
        runbook_result = self.call_skill("runbook_rag", {
            "query": selected["hypothesis"] + " " + selected["category"],
        })
        if runbook_result.success:
            plan.runbook_references = [
                {"id": r["id"], "title": r["title"]}
                for r in runbook_result.output["runbooks"]
            ]

        # CaseRetrieval 检索历史案例的处置经验（优先复用 RCA 阶段已检索结果）
        similar_cases = content.get("similar_cases") or []
        if not similar_cases:
            case_result = self.call_skill("case_retrieval", {
                "query": selected["category"] + " " + selected["hypothesis"] + " 处置 修复",
            })
            if case_result.success:
                similar_cases = case_result.output["cases"]

        # LLM 生成方案说明（上下文注入根因、方案骨架、Runbook 依据与历史案例）
        plan.narrative = self.call_llm(json.dumps(
            {"root_cause": selected["hypothesis"], "category": selected["category"],
             "steps": [s.action for s in plan.steps],
             "runbook_references": [r["title"] for r in plan.runbook_references],
             "similar_case_resolutions": [c["resolution"] for c in similar_cases]},
            ensure_ascii=False,
        ))
        self.logger.info(
            f"  ✦ 修复方案已生成: {len(plan.steps)} 个步骤，风险等级 {plan.risk_level}，"
            f"需审批={plan.approval_required}，回滚步骤 {len(plan.rollback_plan)} 个，"
            f"Runbook 依据 {len(plan.runbook_references)} 条"
        )
        return {**content, "remediation_plan": plan.model_dump()}

    def _build_plan(self, root_cause: Dict[str, Any], service: str) -> RemediationPlan:
        """按根因类别生成方案模板（action_type 对齐 config/action_whitelist.yaml）。"""
        category = root_cause["category"]
        change_id = root_cause.get("related_change_id")

        if category == "db_pool_exhaustion":
            risk = "medium"
            steps = _steps([
                {"action": f"回滚可疑变更 {change_id or '（最近一次发布）'}，下线泄漏代码",
                 "action_type": "rollback_change",
                 "command": f"kubectl rollout undo deployment/{service} -n prod",
                 "expected_effect": "连接泄漏源头消除，池占用逐步回落"},
                {"action": "临时调大连接池上限，缓解恢复期请求排队",
                 "action_type": "scale_pool",
                 "command": f"kubectl set env deployment/{service} -n prod HIKARI_MAXIMUM_POOL_SIZE=50",
                 "expected_effect": "恢复期内连接获取超时减少"},
                {"action": "滚动重启实例，强制释放已泄漏连接",
                 "action_type": "rolling_restart",
                 "command": f"kubectl rollout restart deployment/{service} -n prod",
                 "expected_effect": "存量泄漏连接全部回收，错误率归零"},
            ])
            rollback = _steps([
                {"action": "若回滚后 30 分钟错误率未下降，重新发布当前版本并升级人工介入",
                 "action_type": "rollback_change",
                 "command": f"kubectl rollout undo deployment/{service} -n prod --to-revision=<current>",
                 "expected_effect": "恢复初始状态，保留现场供人工排查"},
                {"action": "恢复连接池参数为原值",
                 "action_type": "config_update",
                 "command": f"kubectl set env deployment/{service} -n prod HIKARI_MAXIMUM_POOL_SIZE=20",
                 "expected_effect": "配置回到基线，避免掩盖问题"},
            ])
        elif category == "container_oom":
            risk = "medium"
            steps = _steps([
                {"action": f"回滚可疑变更 {change_id or '（最近一次发布）'}，移除内存膨胀源",
                 "action_type": "rollback_change",
                 "command": f"kubectl rollout undo deployment/{service} -n prod",
                 "expected_effect": "内存增长曲线恢复平稳"},
                {"action": "临时上调容器内存 limit，提供恢复缓冲",
                 "action_type": "config_update",
                 "command": f"kubectl patch deployment {service} -n prod --patch '{{\"spec\":{{\"template\":{{\"spec\":{{\"containers\":[{{\"name\":\"{service}\",\"resources\":{{\"limits\":{{\"memory\":\"6Gi\"}}}}}}]}}}}}}}}'",
                 "expected_effect": "短期内不再触发 OOMKilled"},
                {"action": "重启后采集 heap dump 留存证据，供复盘分析",
                 "action_type": "diagnostic_capture",
                 "command": f"kubectl exec deploy/{service} -n prod -- jmap -dump:live,file=/tmp/heap.hprof 1",
                 "expected_effect": "获得内存快照，定位具体泄漏对象"},
            ])
            rollback = _steps([
                {"action": "若回滚后仍持续 OOM，恢复内存 limit 原值并升级人工介入",
                 "action_type": "config_update",
                 "command": f"kubectl patch deployment {service} -n prod --patch '{{\"spec\":{{\"template\":{{\"spec\":{{\"containers\":[{{\"name\":\"{service}\",\"resources\":{{\"limits\":{{\"memory\":\"4Gi\"}}}}}}]}}}}}}}}'",
                 "expected_effect": "配置回到基线"},
            ])
        elif category == "network_latency":
            risk = "medium"
            steps = _steps([
                {"action": f"回滚网络变更 {change_id or '（最近一次网络策略调整）'}",
                 "action_type": "rollback_change",
                 "command": "aliyun ecs RevokeSecurityGroup --RegionId cn-hangzhou --SecurityGroupId <sg-id>",
                 "expected_effect": "网络链路恢复变更前状态，延迟回落"},
                {"action": "将网关流量临时切换至可用区 A，绕开受影响链路",
                 "action_type": "traffic_switch",
                 "command": "kubectl annotate svc user-service -n prod topology.kubernetes.io/preferred-zone=cn-hangzhou-a --overwrite",
                 "expected_effect": "用户侧延迟立即缓解"},
                {"action": "联动网络团队核查可用区 B 专线/安全组规则",
                 "action_type": "manual_followup",
                 "command": None,
                 "expected_effect": "确认根因细节，避免复发"},
            ])
            rollback = _steps([
                {"action": "若切流后出现容量问题，恢复默认双可用区路由并升级人工介入",
                 "action_type": "traffic_switch",
                 "command": "kubectl annotate svc user-service -n prod topology.kubernetes.io/preferred-zone- ",
                 "expected_effect": "恢复默认流量分布"},
            ])
        else:
            # 未知类别：仅给出保守的观察与人工升级方案
            risk = "high"
            steps = _steps([
                {"action": "冻结相关服务的发布窗口，防止叠加变更",
                 "action_type": "manual_followup", "command": None,
                 "expected_effect": "故障域不再扩大"},
                {"action": "升级人工介入，SRE 值班认领排查",
                 "action_type": "manual_followup", "command": None,
                 "expected_effect": "由人工确认根因后再制定方案"},
            ])
            rollback = _steps([
                {"action": "解除发布冻结", "action_type": "manual_followup",
                 "command": None, "expected_effect": "恢复正常发布节奏"},
            ])

        return RemediationPlan(
            plan_id=f"PLAN-{uuid.uuid4().hex[:8].upper()}",
            root_cause_category=category,
            risk_level=risk,
            approval_required=_APPROVAL_POLICY[risk],
            steps=steps,
            rollback_plan=rollback,
        )
