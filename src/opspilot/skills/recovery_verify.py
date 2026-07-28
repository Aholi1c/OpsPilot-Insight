# -*- coding: utf-8 -*-
"""RecoveryVerify Skill：指标对比验证（VerifierAgent 用）。

对比故障期指标（metrics.json）与修复后指标（metrics_after.json）：
1. 异常判定：故障期序列尾部相对头部（基线）明显劣化的指标视为异常指标
   - 越大越差类（延迟/错误数/资源占用）：尾/头 >= 1.5 判异常；
   - 越小越差类（成功率等）：尾/头 <= 0.9 判异常；
2. 恢复判定：修复后序列尾部回归基线邻域
   - 越大越差类：after_tail <= baseline * 1.3 + 0.5；
   - 越小越差类：after_tail >= baseline * 0.95。

告警消除判定：全部异常指标均已恢复 => 告警条件消除。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .base import Skill, SkillContext


def _series_head_tail(metric: Dict[str, Any]) -> Optional[Dict[str, float]]:
    """取时间序列首尾值（首值视为基线，尾值视为当前状态）。"""
    points = metric.get("points", [])
    if len(points) < 2:
        return None
    return {"head": float(points[0]["value"]), "tail": float(points[-1]["value"])}


class RecoveryVerifySkill(Skill):
    name = "recovery_verify"
    version = "1.0.0"
    description = "恢复验证：对比故障期与修复后指标，判定告警是否消除、指标是否回归基线"
    input_schema = {
        "incident": "Incident 序列化字典（用于圈定关注服务，可为空则全量对比）",
    }
    output_schema = {
        "passed": "验证是否通过（全部异常指标恢复）",
        "alerts_cleared": "告警条件是否消除",
        "checks": "逐指标验证明细（baseline/before_fix/after_fix/recovered）",
        "summary": "验证结论摘要",
    }
    preconditions = ["incident"]
    failure_policy = "degrade"  # 验证失败时报告标记"未验证"，不阻断复盘

    def run(self, payload: Dict[str, Any], context: SkillContext) -> Dict[str, Any]:
        before_metrics = context.adapters["monitoring"].query_metrics()
        after_metrics = context.adapters["monitoring_after"].query_metrics()
        after_index = {(m.get("name"), m.get("service")): m for m in after_metrics}

        checks: List[Dict[str, Any]] = []
        for metric in before_metrics:
            ht = _series_head_tail(metric)
            if ht is None:
                continue
            baseline, before_tail = ht["head"], ht["tail"]

            # ---- 异常判定：只验证故障期确实劣化的指标 ----
            if baseline > 0:
                ratio = before_tail / baseline
                if ratio >= 1.5:
                    direction = "up"      # 越大越差（延迟/错误/占用）
                elif ratio <= 0.9:
                    direction = "down"    # 越小越差（成功率）
                else:
                    continue
            elif before_tail > 0:
                direction = "up"          # 基线为 0 且出现增长（如超时计数）
            else:
                continue

            # ---- 恢复判定：修复后序列尾部回归基线邻域 ----
            after = after_index.get((metric.get("name"), metric.get("service")))
            after_ht = _series_head_tail(after) if after else None
            if after_ht is None:
                checks.append({
                    "metric": metric.get("name", ""), "service": metric.get("service", ""),
                    "unit": metric.get("unit", ""), "baseline": baseline,
                    "before_fix": before_tail, "after_fix": before_tail, "recovered": False,
                })
                continue
            after_tail = after_ht["tail"]
            if direction == "up":
                recovered = after_tail <= baseline * 1.3 + 0.5
            else:
                recovered = after_tail >= baseline * 0.95
            checks.append({
                "metric": metric.get("name", ""), "service": metric.get("service", ""),
                "unit": metric.get("unit", ""), "baseline": baseline,
                "before_fix": before_tail, "after_fix": after_tail, "recovered": recovered,
            })

        recovered_count = sum(1 for c in checks if c["recovered"])
        alerts_cleared = bool(checks) and recovered_count == len(checks)
        passed = alerts_cleared
        summary = (
            f"验证{'通过' if passed else '未通过'}：{len(checks)} 项异常指标中 "
            f"{recovered_count} 项已回归基线，告警条件{'已消除' if alerts_cleared else '仍存在'}"
        )
        context.logger.info(f"    ✓ {summary}" if passed else f"    ✗ {summary}")
        return {
            "passed": passed,
            "alerts_cleared": alerts_cleared,
            "checks": checks,
            "summary": summary,
        }
