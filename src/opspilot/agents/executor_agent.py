# -*- coding: utf-8 -*-
"""ExecutorAgent：安全执行 Agent。

职责：接收 PlannerAgent 审定的修复方案，在安全边界内 Mock 执行：
1. RiskGuard 执行前风险评估（动作风险 + 影响半径 + 审批判定）；
2. 人工审批交互点：medium/high 风险方案必须审批（回调由入口注入，
   缺省自动批准，审批决定 who/when/decision 写入审计日志）；
3. SafeExecute 安全执行：白名单校验 + 幂等键 + 回滚检查点 + 失败自动回滚。

本 Agent 不调用 LLM（执行链路要求确定性），全程审计事件留痕。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from ..models import ApprovalRecord, ExecutionResult
from .base import BaseAgent


class ExecutorAgent(BaseAgent):
    config_key = "executor_agent"

    def output_message_type(self) -> str:
        return "execution_result"

    def process(self, content: Dict[str, Any]) -> Dict[str, Any]:
        plan: Optional[Dict[str, Any]] = content.get("remediation_plan")
        incident: Dict[str, Any] = content["incident"]
        if not plan:
            # 上游未产出方案（降级/转人工），执行阶段按跳过处理
            self.logger.warning("无修复方案可执行，执行阶段跳过（等待人工方案）")
            return {**content, "execution_result": ExecutionResult(
                plan_id="", executed=False, status="skipped",
                notes=["上游未产出修复方案，执行阶段跳过"],
            ).model_dump()}

        # ---- 1. 执行前风险评估（评估失败按最保守策略：需审批）----
        risk_result = self.call_skill("risk_guard", {"plan": plan, "incident": incident})
        if risk_result.success:
            risk = risk_result.output
        else:
            risk = {"overall_risk": plan.get("risk_level", "high"), "needs_approval": True,
                    "degraded": True}

        # ---- 2. 人工审批交互点（决定 who/when/decision 写入审计）----
        approval = self._request_approval(plan, risk, incident)
        if self.skill_context.audit:
            self.skill_context.audit.record(
                "approval", incident_id=incident.get("incident_id"),
                plan_id=plan.get("plan_id"), required=approval.required,
                approved=approval.approved, approver=approval.approver,
                decided_at=approval.decided_at, mode=approval.mode, reason=approval.reason,
            )
        if not approval.approved:
            self.logger.warning(f"  ⛔ 方案 {plan.get('plan_id')} 审批被拒绝，不执行任何动作")
            return {**content, "execution_result": ExecutionResult(
                plan_id=plan.get("plan_id", ""), executed=False, status="rejected",
                approval=approval, risk_assessment=risk,
                notes=[f"审批被拒绝（{approval.approver}）: {approval.reason or '无理由说明'}"],
            ).model_dump()}
        self.logger.info(
            f"  ✔ 审批通过（{approval.approver} / {approval.mode}），开始安全执行"
            if approval.required else "  ✔ 低风险方案免审批，开始安全执行"
        )

        # ---- 3. 安全执行（白名单 + 幂等 + 检查点 + 失败自动回滚）----
        exec_result = self.call_skill("safe_execute", {
            "incident_id": incident.get("incident_id", ""),
            "plan": plan,
        })
        output = exec_result.output
        result = ExecutionResult(
            plan_id=plan.get("plan_id", ""),
            executed=output["executed"],
            status=output["status"],
            approval=approval,
            risk_assessment=risk,
            actions=output["actions"],
            rollback_performed=output["rollback_performed"],
            rollbacks=output["rollbacks"],
        )
        if output["rollback_performed"]:
            result.notes.append("存在执行失败动作，已按检查点逆序自动回滚并续行备选动作")
        self.logger.info(
            f"  ✦ 执行完成: 状态={result.status}，动作 {len(result.actions)} 个，"
            f"回滚 {len(result.rollbacks)} 次"
        )
        return {**content, "execution_result": result.model_dump()}

    def _request_approval(
        self, plan: Dict[str, Any], risk: Dict[str, Any], incident: Dict[str, Any],
    ) -> ApprovalRecord:
        """审批交互点：回调由入口注入（run_demo 交互式 / 测试与 --auto-approve 自动）。"""
        needs_approval = bool(risk.get("needs_approval", True))
        if not needs_approval:
            return ApprovalRecord(required=False, approved=True,
                                  approver="policy", mode="policy", reason="低风险方案免审批")
        handler = self.skill_context.extras.get("approval_handler")
        if handler is None:
            # 缺省策略：自动批准（--auto-approve / 测试路径）
            return ApprovalRecord(required=True, approved=True, approver="auto-approve",
                                  mode="auto", reason="自动审批模式（--auto-approve）")
        decision = handler(plan, risk, incident)
        return ApprovalRecord(
            required=True,
            approved=bool(decision.get("approved", False)),
            approver=decision.get("approver", "unknown"),
            mode=decision.get("mode", "interactive"),
            reason=decision.get("reason", ""),
        )
