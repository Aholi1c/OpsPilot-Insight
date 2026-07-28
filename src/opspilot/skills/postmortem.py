# -*- coding: utf-8 -*-
"""Postmortem Skill：复盘报告生成（VerifierAgent 用）。

汇总一次事故处置全程：时间线（流水线时间线 + 审计关键事件）、根因、
处置动作、恢复效果与改进建议（规则生成），并组装可沉淀到知识库的
"历史案例"文档（案例写入由 VerifierAgent 调用 KnowledgeStore 完成）。
"""
from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional

from .base import Skill, SkillContext


class PostmortemSkill(Skill):
    name = "postmortem"
    version = "1.0.0"
    description = "复盘报告生成：时间线 + 根因 + 处置动作 + 效果 + 改进建议，并组装知识库案例"
    input_schema = {
        "incident": "Incident 序列化字典",
        "root_cause": "选定根因候选（可为 None）",
        "plan": "RemediationPlan 序列化字典（可为 None）",
        "execution_result": "ExecutionResult 序列化字典（可为 None）",
        "verification": "RecoveryVerify 输出（可为 None）",
        "pipeline_timeline": "Orchestrator 流水线时间线",
    }
    output_schema = {
        "timeline": "复盘时间线（流水线阶段 + 审计关键事件）",
        "root_cause": "根因描述",
        "actions_taken": "实际处置动作清单",
        "effect": "处置效果描述",
        "improvements": "改进建议清单",
        "case": "可沉淀到知识库的历史案例文档",
    }
    preconditions = ["incident"]
    failure_policy = "degrade"  # 复盘失败不影响报告主体产出

    def run(self, payload: Dict[str, Any], context: SkillContext) -> Dict[str, Any]:
        incident: Dict[str, Any] = payload["incident"]
        root_cause: Optional[Dict[str, Any]] = payload.get("root_cause")
        plan: Optional[Dict[str, Any]] = payload.get("plan")
        execution: Optional[Dict[str, Any]] = payload.get("execution_result")
        verification: Optional[Dict[str, Any]] = payload.get("verification")

        timeline = self._build_timeline(payload.get("pipeline_timeline", []), context)
        root_cause_text = (
            root_cause.get("hypothesis", "") if root_cause else "未定位到可信根因（转人工排查）"
        )
        actions_taken, resolution_minutes = self._summarize_actions(execution)
        effect = self._summarize_effect(execution, verification)
        improvements = self._build_improvements(root_cause, execution, verification)

        # 组装知识库案例：症状/根因/处置/耗时，供后续 CaseRetrieval 命中
        case = {
            "id": f"CASE-{uuid.uuid4().hex[:8].upper()}",
            "type": "case",
            "incident_id": incident.get("incident_id", ""),
            "title": incident.get("title", ""),
            "category": root_cause.get("category", "unknown") if root_cause else "unknown",
            "services": incident.get("affected_services", []),
            "symptoms": incident.get("summary", ""),
            "root_cause": root_cause_text,
            "resolution": "；".join(actions_taken) if actions_taken else "未执行自动处置",
            "duration_minutes": resolution_minutes,
            "keywords": incident.get("affected_services", [])
            + ([root_cause.get("category", "")] if root_cause else []),
        }
        context.logger.info(
            f"    ✦ 复盘完成: 时间线 {len(timeline)} 项，处置动作 {len(actions_taken)} 个，"
            f"改进建议 {len(improvements)} 条"
        )
        return {
            "timeline": timeline,
            "root_cause": root_cause_text,
            "actions_taken": actions_taken,
            "effect": effect,
            "improvements": improvements,
            "case": case,
        }

    @staticmethod
    def _build_timeline(
        pipeline_timeline: List[Dict[str, str]], context: SkillContext,
    ) -> List[Dict[str, str]]:
        """流水线阶段时间线 + 审计关键事件（审批/执行失败/回滚）合并。"""
        timeline = [dict(item) for item in pipeline_timeline]
        for event in (context.audit.events if context.audit else []):
            event_type = event.get("event_type", "")
            if event_type == "approval":
                timeline.append({
                    "time": event.get("timestamp", ""),
                    "event": f"审批{'通过' if event.get('approved') else '拒绝'}"
                             f"（{event.get('approver', '')} / {event.get('mode', '')}）",
                })
            elif event_type == "execute" and event.get("status") == "failed":
                timeline.append({
                    "time": event.get("timestamp", ""),
                    "event": f"动作 #{event.get('action_order')} 执行失败: {event.get('detail', '')}",
                })
            elif event_type == "rollback":
                timeline.append({
                    "time": event.get("timestamp", ""),
                    "event": f"自动回滚检查点 {event.get('checkpoint_id', '')}",
                })
        return sorted(timeline, key=lambda x: x.get("time", ""))

    @staticmethod
    def _summarize_actions(execution: Optional[Dict[str, Any]]) -> Any:
        """从执行结果提取实际处置动作与总耗时（分钟，Mock 场景向上取整）。"""
        if not execution:
            return [], 0
        actions: List[str] = []
        total_ms = 0.0
        for item in execution.get("actions", []):
            total_ms += item.get("duration_ms", 0.0)
            status = item.get("status")
            if status in ("success", "manual"):
                prefix = "[备选] " if item.get("fallback") else ""
                actions.append(prefix + item.get("action", ""))
            elif status == "failed":
                actions.append(f"[失败后回滚] {item.get('action', '')}")
        return actions, max(1, int(total_ms / 60000) + 1)

    @staticmethod
    def _summarize_effect(
        execution: Optional[Dict[str, Any]], verification: Optional[Dict[str, Any]],
    ) -> str:
        if not execution or not execution.get("executed"):
            return "未执行自动处置（方案被拒绝或降级），效果待人工确认"
        parts = []
        if execution.get("rollback_performed"):
            parts.append("首选动作失败后自动回滚检查点，备选动作接管处置")
        if verification:
            parts.append(verification.get("summary", ""))
        else:
            parts.append("恢复验证未完成，效果待确认")
        return "；".join(p for p in parts if p)

    @staticmethod
    def _build_improvements(
        root_cause: Optional[Dict[str, Any]],
        execution: Optional[Dict[str, Any]],
        verification: Optional[Dict[str, Any]],
    ) -> List[str]:
        """规则生成改进建议：按根因类别 + 本次执行暴露的问题。"""
        improvements: List[str] = []
        category = root_cause.get("category", "") if root_cause else ""
        by_category = {
            "db_pool_exhaustion": [
                "上线前压测覆盖连接池水位场景，发布流水线增加连接泄漏静态检查",
                "为连接池使用率配置分级告警（80% 预警 / 95% 紧急），缩短发现时间",
            ],
            "container_oom": [
                "发布流水线增加内存基线对比，容器内存 limit 变更纳入容量评审",
                "为容器 WorkingSet/limit 比例配置预警，OOMKilled 事件自动关联最近变更",
            ],
            "network_latency": [
                "网络 ACL / 安全组变更纳入变更管控平台，强制灰度并同步基线策略",
                "为跨可用区链路配置端到端拨测，缩短网络劣化的发现时间",
            ],
        }
        improvements.extend(by_category.get(category, ["根因未收敛，安排专项人工复盘"]))
        if execution:
            if execution.get("rollback_performed"):
                improvements.append(
                    "首选处置动作执行失败（权限/基线策略拦截），建议预先核查执行账号权限与策略锁定状态"
                )
            if any(a.get("status") == "rejected_whitelist" for a in execution.get("actions", [])):
                improvements.append("方案包含白名单外动作类型，需评审后纳入白名单或调整方案模板")
        if verification and not verification.get("passed"):
            improvements.append("恢复验证未通过，需人工确认残留异常指标并补充处置")
        return improvements
