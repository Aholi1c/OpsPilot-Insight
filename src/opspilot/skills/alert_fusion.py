# -*- coding: utf-8 -*-
"""AlertFusion Skill：告警去重（按 fingerprint）+ 聚合（按 alertname+service）。

将 AlertManager 风格的原始告警流收敛为少量告警组，并给出事件级别的
严重度与首告警时间，是后续影响面评估与根因分析的入口。
"""
from __future__ import annotations

from typing import Any, Dict, List

from ..models import Alert
from .base import Skill, SkillContext

# 严重度排序（越靠前越严重）
_SEVERITY_ORDER = ["critical", "error", "warning", "info"]


class AlertFusionSkill(Skill):
    name = "alert_fusion"
    version = "1.0.0"
    description = "告警去重与聚合：fingerprint 去重 + (alertname, service) 分组"
    input_schema = {"alerts": "AlertManager 格式的原始告警列表"}
    output_schema = {
        "raw_count": "原始告警数",
        "deduped_count": "去重后告警数",
        "groups": "聚合后的告警组列表",
        "services": "涉及服务列表",
        "severity": "事件级严重度（取各组最高）",
        "first_alert_at": "最早告警时间",
    }
    preconditions = ["alerts"]
    failure_policy = "abort"  # 入口 Skill 失败则整个流水线无意义，直接上抛

    def run(self, payload: Dict[str, Any], context: SkillContext) -> Dict[str, Any]:
        alerts = [Alert.model_validate(a) for a in payload["alerts"]]

        # 1. 按 fingerprint 去重（AlertManager 会重复推送同一告警）
        deduped: Dict[str, Alert] = {}
        for alert in alerts:
            deduped.setdefault(alert.fingerprint, alert)

        # 2. 按 (alertname, service) 聚合成组
        grouped: Dict[tuple, List[Alert]] = {}
        for alert in deduped.values():
            grouped.setdefault((alert.alertname, alert.service), []).append(alert)

        groups = []
        for (alertname, service), members in grouped.items():
            severities = sorted(
                (m.severity for m in members),
                key=lambda s: _SEVERITY_ORDER.index(s) if s in _SEVERITY_ORDER else 99,
            )
            groups.append({
                "alertname": alertname,
                "service": service,
                "severity": severities[0],
                "count": len(members),
                "first_starts_at": min(m.starts_at for m in members),
                "summary": members[0].annotations.get("summary", ""),
            })
        # 按严重度排序，最严重的组排最前
        groups.sort(key=lambda g: _SEVERITY_ORDER.index(g["severity"]) if g["severity"] in _SEVERITY_ORDER else 99)

        services = sorted({g["service"] for g in groups})
        context.logger.info(
            f"    告警收敛: {len(alerts)} 条原始告警 -> {len(deduped)} 条去重 -> {len(groups)} 个告警组",
            services=",".join(services),
        )
        return {
            "raw_count": len(alerts),
            "deduped_count": len(deduped),
            "groups": groups,
            "services": services,
            "severity": groups[0]["severity"] if groups else "info",
            "first_alert_at": min(g["first_starts_at"] for g in groups) if groups else "",
        }
