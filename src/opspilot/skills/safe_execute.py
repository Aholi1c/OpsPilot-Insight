# -*- coding: utf-8 -*-
"""SafeExecute Skill：白名单 + 幂等 + 回滚检查点的安全执行框架（ExecutorAgent 用）。

安全边界：
1. 白名单校验：动作类型不在 config/action_whitelist.yaml 内一律拒绝并记录审计事件；
2. 幂等键：incident_id + 动作内容哈希，同 key 动作重复执行直接跳过并记录；
3. 回滚检查点：每个动作执行前记录当前状态快照，失败时按检查点逆序自动回滚；
4. 失败续行：回滚完成后继续执行剩余步骤（标记为备选动作 fallback），
   实现"首选动作失败 -> 自动回滚 -> 备选动作"的处置链路。

全部关键节点（whitelist_check / checkpoint / execute / rollback）写入审计事件流。
"""
from __future__ import annotations

import hashlib
import time
from typing import Any, Dict, List, Optional

from .base import Skill, SkillContext


def make_idempotency_key(incident_id: str, action_type: str, action: str, command: Optional[str]) -> str:
    """幂等键：incident_id + 动作内容哈希（同一事件内相同动作只执行一次）。"""
    raw = f"{incident_id}|{action_type}|{action}|{command or ''}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


class SafeExecuteSkill(Skill):
    name = "safe_execute"
    version = "1.0.0"
    description = "安全执行框架：白名单校验 + 幂等键 + 回滚检查点 + 失败自动回滚"
    input_schema = {
        "incident_id": "事件 ID（幂等键的组成部分）",
        "plan": "RemediationPlan 序列化字典（steps 携带 action_type）",
    }
    output_schema = {
        "status": "success / success_with_rollback / failed / rejected",
        "executed": "是否实际执行了至少一个动作",
        "actions": "逐动作执行记录（含幂等键/检查点/状态）",
        "rollbacks": "回滚记录（逆序恢复检查点）",
        "rollback_performed": "是否触发了自动回滚",
    }
    preconditions = ["incident_id", "plan"]
    failure_policy = "abort"  # 执行框架自身异常由 Orchestrator 统一降级

    def run(self, payload: Dict[str, Any], context: SkillContext) -> Dict[str, Any]:
        incident_id: str = payload["incident_id"]
        plan: Dict[str, Any] = payload["plan"]
        whitelist: Dict[str, Any] = context.extras.get("action_whitelist", {})
        execution = context.adapters["execution"]
        # 幂等注册表挂在流水线级上下文上：同一事件内重复动作直接跳过
        registry = context.extras.setdefault("idempotency_registry", set())

        actions: List[Dict[str, Any]] = []
        rollbacks: List[Dict[str, Any]] = []
        checkpoints: List[Dict[str, Any]] = []  # (checkpoint, step) 成功动作的检查点栈
        rollback_performed = False
        in_fallback = False  # 首选动作失败回滚后，后续步骤标记为备选动作

        for step in plan.get("steps", []):
            order = step.get("order", 0)
            action = step.get("action", "")
            action_type = step.get("action_type", "")
            command = step.get("command")

            # ---- 1. 白名单校验 ----
            allowed = action_type in whitelist
            if context.audit:
                context.audit.record(
                    "whitelist_check", incident_id=incident_id, plan_id=plan.get("plan_id"),
                    action_order=order, action_type=action_type, allowed=allowed,
                    detail=action if allowed else f"动作类型 [{action_type}] 不在白名单，拒绝执行: {action}",
                )
            if not allowed:
                context.logger.warning(
                    f"    ✗ 动作 #{order} 类型 [{action_type}] 不在白名单，已拒绝", action=action,
                )
                actions.append(self._action_record(
                    step, "", "rejected_whitelist",
                    f"动作类型 [{action_type}] 不在白名单", None, in_fallback,
                ))
                continue

            # ---- 2. 幂等键校验 ----
            idem_key = make_idempotency_key(incident_id, action_type, action, command)
            if idem_key in registry:
                if context.audit:
                    context.audit.record(
                        "execute", incident_id=incident_id, plan_id=plan.get("plan_id"),
                        action_order=order, action_type=action_type,
                        idempotency_key=idem_key, status="skipped_idempotent",
                        detail=f"幂等键已存在，跳过重复执行: {action}",
                    )
                context.logger.info(f"    ↷ 动作 #{order} 幂等键命中，跳过重复执行", idempotency_key=idem_key)
                actions.append(self._action_record(
                    step, idem_key, "skipped_idempotent", "幂等键命中，跳过重复执行", None, in_fallback,
                ))
                continue

            # ---- 人工跟进类动作：不操作资源，登记任务即完成 ----
            if action_type == "manual_followup":
                registry.add(idem_key)
                if context.audit:
                    context.audit.record(
                        "execute", incident_id=incident_id, plan_id=plan.get("plan_id"),
                        action_order=order, action_type=action_type,
                        idempotency_key=idem_key, status="manual",
                        detail=f"已创建人工跟进任务: {action}",
                    )
                actions.append(self._action_record(
                    step, idem_key, "manual", "已创建人工跟进任务（不直接操作生产资源）", None, in_fallback,
                ))
                continue

            # ---- 3. 回滚检查点 + 执行 ----
            with context.tracer.start_span(
                f"execute.step_{order}", kind="CLIENT",
                attributes={"action.order": order, "action.type": action_type,
                            "action.fallback": in_fallback, "idempotency_key": idem_key},
            ) as span:
                started = time.perf_counter()
                checkpoint = execution.create_checkpoint(action_type, target=action)
                if context.audit:
                    context.audit.record(
                        "checkpoint", incident_id=incident_id, plan_id=plan.get("plan_id"),
                        action_order=order, checkpoint_id=checkpoint["checkpoint_id"],
                        snapshot=checkpoint["snapshot"],
                    )
                result = execution.execute_action(action_type, action, command, order=order)
                duration_ms = round((time.perf_counter() - started) * 1000, 2)
                span.set_attribute("action.status", result["status"])
                if context.audit:
                    context.audit.record(
                        "execute", incident_id=incident_id, plan_id=plan.get("plan_id"),
                        action_order=order, action_type=action_type,
                        idempotency_key=idem_key, status=result["status"],
                        checkpoint_id=checkpoint["checkpoint_id"], detail=result["message"],
                    )

                if result["status"] == "success":
                    registry.add(idem_key)
                    checkpoints.append({"checkpoint": checkpoint, "step": step})
                    context.logger.info(
                        f"    ✓ 动作 #{order} 执行成功{'（备选动作）' if in_fallback else ''}: {action}",
                    )
                    actions.append(self._action_record(
                        step, idem_key, "success", result["message"],
                        checkpoint["checkpoint_id"], in_fallback, duration_ms,
                    ))
                else:
                    # ---- 4. 执行失败：按检查点逆序自动回滚，剩余步骤转备选 ----
                    span.status = "ERROR"
                    span.status_message = result["message"]
                    context.logger.error(f"    ✗ 动作 #{order} 执行失败: {result['message']}")
                    actions.append(self._action_record(
                        step, idem_key, "failed", result["message"],
                        checkpoint["checkpoint_id"], in_fallback, duration_ms,
                    ))
                    rollbacks.extend(self._rollback(
                        [{"checkpoint": checkpoint, "step": step}] + checkpoints[::-1],
                        incident_id, plan, execution, context,
                    ))
                    checkpoints.clear()
                    rollback_performed = True
                    in_fallback = True

        executed = any(a["status"] in ("success", "manual") for a in actions)
        if not executed and actions and all(a["status"] == "rejected_whitelist" for a in actions):
            status = "rejected"
        elif rollback_performed:
            status = "success_with_rollback" if executed else "failed"
        else:
            status = "success" if executed else "failed"
        return {
            "status": status,
            "executed": executed,
            "actions": actions,
            "rollbacks": rollbacks,
            "rollback_performed": rollback_performed,
        }

    def _rollback(
        self,
        stack: List[Dict[str, Any]],
        incident_id: str,
        plan: Dict[str, Any],
        execution: Any,
        context: SkillContext,
    ) -> List[Dict[str, Any]]:
        """按检查点逆序回滚（失败动作的检查点最先恢复），全程带 Trace 与审计。"""
        records: List[Dict[str, Any]] = []
        with context.tracer.start_span(
            "execute.rollback", kind="CLIENT",
            attributes={"rollback.checkpoint_count": len(stack)},
        ):
            context.logger.warning(f"    ⟲ 触发自动回滚：逆序恢复 {len(stack)} 个检查点")
            for item in stack:
                checkpoint, step = item["checkpoint"], item["step"]
                result = execution.rollback_to(checkpoint)
                if context.audit:
                    context.audit.record(
                        "rollback", incident_id=incident_id, plan_id=plan.get("plan_id"),
                        action_order=step.get("order"), checkpoint_id=checkpoint["checkpoint_id"],
                        status=result["status"], detail=result["message"],
                    )
                records.append({
                    "checkpoint_id": checkpoint["checkpoint_id"],
                    "action_order": step.get("order", 0),
                    "action": step.get("action", ""),
                    "status": result["status"],
                    "message": result["message"],
                })
        return records

    @staticmethod
    def _action_record(
        step: Dict[str, Any], idem_key: str, status: str, message: str,
        checkpoint_id: Optional[str], fallback: bool, duration_ms: float = 0.0,
    ) -> Dict[str, Any]:
        return {
            "order": step.get("order", 0),
            "action": step.get("action", ""),
            "action_type": step.get("action_type", ""),
            "command": step.get("command"),
            "idempotency_key": idem_key,
            "status": status,
            "message": message,
            "checkpoint_id": checkpoint_id,
            "fallback": fallback,
            "duration_ms": duration_ms,
        }
