# -*- coding: utf-8 -*-
"""LogTraceRca Skill：多维根因分析（日志 + 链路 + 指标 + 变更关联）。

分析流程：
1. 日志维度：按故障模式关键词库对错误日志分类计数，识别主导故障模式；
2. 指标维度：对比时间序列首尾均值检测异常爬升/劣化；
3. 链路维度：提取错误 span 与慢 span，定位瓶颈服务与操作；
4. 变更维度：检索告警前时间窗内的变更记录，与受影响服务做关联；
5. 综合以上信号生成根因候选（假设 + 证据链 + 证据强度 strong/weak/missing + 置信度）。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from .base import Skill, SkillContext

# 故障模式知识库：日志关键词（按优先级匹配，一条日志只归入首个命中的模式）
_FAILURE_PATTERNS = [
    {
        "category": "db_pool_exhaustion",
        "log_keywords": [
            "Connection is not available", "HikariPool", "connection pool",
            "SQLTransientConnectionException",
        ],
        "metric_hint": ("pool", "connection", "error_rate"),  # 指标名命中任一视为强相关
        "slow_op_hint": ("getConnection", "sql", "jdbc"),
    },
    {
        "category": "container_oom",
        "log_keywords": [
            "OutOfMemoryError", "OOMKilled", "oom-killer", "Out of memory",
            "CrashLoopBackOff",
        ],
        "metric_hint": ("memory", "heap", "restart"),
        "slow_op_hint": (),
    },
    {
        "category": "network_latency",
        "log_keywords": [
            "upstream timed out", "Read timed out", "Connection timed out",
            "deadline exceeded",
        ],
        "metric_hint": ("latency", "duration", "p99", "rt"),
        "slow_op_hint": ("http", "grpc", "call"),
    },
]

# 变更关联时间窗：告警前 4 小时内的变更视为可疑
_CHANGE_WINDOW_HOURS = 4
# 慢 span 阈值（毫秒）
_SLOW_SPAN_MS = 800
# 指标异常判定：末段均值 / 首段均值 超过该倍数视为异常劣化
_ANOMALY_RATIO = 1.5

# 各模式的根因假设模板
_HYPOTHESIS_TEMPLATES = {
    "db_pool_exhaustion": {
        "with_change": "{service} 数据库连接池耗尽，疑似由变更 {change_id}（{change_title}）引入连接泄漏导致",
        "no_change": "{service} 数据库连接池耗尽，连接获取超时导致请求大面积失败",
    },
    "container_oom": {
        "with_change": "{service} 容器内存持续增长触发 OOMKilled，疑似由变更 {change_id}（{change_title}）引入内存膨胀导致",
        "no_change": "{service} 容器内存持续增长触发 OOMKilled，疑似内存泄漏或堆内存配置不足",
    },
    "network_latency": {
        "with_change": "{service} 调用链网络延迟劣化（服务端处理正常），疑似由变更 {change_id}（{change_title}）引入网络链路问题导致",
        "no_change": "{service} 调用链网络延迟劣化（服务端处理正常），疑似网络链路质量问题",
    },
}


def _parse_time(value: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None


class LogTraceRcaSkill(Skill):
    name = "log_trace_rca"
    version = "1.0.0"
    description = "多维根因分析：日志+链路+指标+变更关联，输出根因候选与证据链"
    input_schema = {
        "affected_services": "受影响服务列表",
        "first_alert_at": "最早告警时间（ISO8601）",
    }
    output_schema = {
        "candidates": "根因候选列表（假设/置信度/证据链）",
        "signals": "四个维度的原始分析信号（便于复盘与可解释性）",
    }
    preconditions = ["affected_services", "first_alert_at"]
    failure_policy = "abort"  # 根因分析失败由 Orchestrator 统一降级

    def run(self, payload: Dict[str, Any], context: SkillContext) -> Dict[str, Any]:
        services: List[str] = payload["affected_services"]
        first_alert = _parse_time(payload["first_alert_at"])

        log_signal = self._analyze_logs(context)
        metric_signal = self._analyze_metrics(context)
        trace_signal = self._analyze_traces(context)
        change_signal = self._analyze_changes(context, services, first_alert)

        candidates = self._build_candidates(
            services, log_signal, metric_signal, trace_signal, change_signal, context,
        )
        return {
            "candidates": candidates,
            "signals": {
                "logs": log_signal,
                "metrics": metric_signal,
                "traces": trace_signal,
                "changes": change_signal,
            },
        }

    # ---- 维度 1：日志 ----
    def _analyze_logs(self, context: SkillContext) -> Dict[str, Any]:
        """错误日志按故障模式分类计数，识别主导模式。"""
        logging_adapter = context.adapters["logging"]
        error_logs = [
            entry for entry in logging_adapter.query_logs()
            if entry.get("level") in ("ERROR", "FATAL", "WARN")
        ]
        matches: Dict[str, List[str]] = {}
        service_hits: Dict[str, Dict[str, int]] = {}  # 模式 -> {服务: 命中数}，用于定位主嫌疑服务
        for entry in error_logs:
            message = entry.get("message", "")
            for pattern in _FAILURE_PATTERNS:
                if any(kw in message for kw in pattern["log_keywords"]):
                    category = pattern["category"]
                    matches.setdefault(category, []).append(
                        f"[{entry.get('service')}] {message}"
                    )
                    per_service = service_hits.setdefault(category, {})
                    per_service[entry.get("service", "unknown")] = per_service.get(entry.get("service", "unknown"), 0) + 1
                    break  # 一条日志只归入优先级最高的模式
        dominant = max(matches, key=lambda c: len(matches[c]), default=None)
        context.logger.info(
            f"    日志分析: {len(error_logs)} 条异常日志，主导模式={dominant}",
            matched={c: len(v) for c, v in matches.items()},
        )
        return {
            "error_log_count": len(error_logs), "matches": matches,
            "dominant": dominant, "service_hits": service_hits,
        }

    # ---- 维度 2：指标 ----
    def _analyze_metrics(self, context: SkillContext) -> Dict[str, Any]:
        """时间序列首尾均值对比，检测异常爬升/劣化。"""
        monitoring = context.adapters["monitoring"]
        anomalies: List[Dict[str, Any]] = []
        for metric in monitoring.query_metrics():
            points = metric.get("points", [])
            if len(points) < 4:
                continue
            values = [float(p["value"]) for p in points]
            head = sum(values[:3]) / 3
            tail = sum(values[-3:]) / 3
            ratio = tail / head if head > 0 else float("inf")
            if ratio >= _ANOMALY_RATIO:
                anomalies.append({
                    "name": metric.get("name"),
                    "service": metric.get("service"),
                    "baseline": round(head, 2),
                    "current": round(tail, 2),
                    "ratio": round(ratio, 2) if ratio != float("inf") else "inf",
                    "unit": metric.get("unit", ""),
                })
        context.logger.info(
            f"    指标分析: 检出 {len(anomalies)} 项异常指标",
            anomalies=[a["name"] for a in anomalies],
        )
        return {"anomalies": anomalies}

    # ---- 维度 3：链路 ----
    def _analyze_traces(self, context: SkillContext) -> Dict[str, Any]:
        """提取错误 span 与慢 span，定位瓶颈服务/操作。"""
        tracing = context.adapters["tracing"]
        slow_spans: List[Dict[str, Any]] = []
        error_spans: List[Dict[str, Any]] = []
        for trace in tracing.query_traces():
            for span in trace.get("spans", []):
                brief = {
                    "service": span.get("service"),
                    "operation": span.get("operation"),
                    "duration_ms": span.get("duration_ms"),
                    "status": span.get("status"),
                }
                if span.get("status") == "ERROR":
                    error_spans.append(brief)
                elif float(span.get("duration_ms", 0)) >= _SLOW_SPAN_MS:
                    slow_spans.append(brief)
        context.logger.info(
            f"    链路分析: 错误 span {len(error_spans)} 个，慢 span {len(slow_spans)} 个",
        )
        return {"slow_spans": slow_spans, "error_spans": error_spans}

    # ---- 维度 4：变更 ----
    def _analyze_changes(
        self,
        context: SkillContext,
        services: List[str],
        first_alert: Optional[datetime],
    ) -> Dict[str, Any]:
        """检索告警前时间窗内的变更并与受影响服务关联。"""
        change_adapter = context.adapters["change"]
        since = (first_alert - timedelta(hours=_CHANGE_WINDOW_HOURS)).isoformat() if first_alert else None
        until = first_alert.isoformat() if first_alert else None
        in_window = change_adapter.query_changes(since=since, until=until)
        related = [c for c in in_window if c.get("service") in services]
        # 网络类变更不挂在业务服务名下，单独识别（对 network_latency 模式是强证据）
        network_changes = [c for c in in_window if c.get("type") == "network"]
        context.logger.info(
            f"    变更分析: 时间窗内 {len(in_window)} 条变更，服务相关 {len(related)} 条",
            related=[c.get("change_id") for c in related],
        )
        return {"in_window": in_window, "related": related, "network_changes": network_changes}

    # ---- 综合：生成根因候选 ----
    def _build_candidates(
        self,
        services: List[str],
        log_signal: Dict[str, Any],
        metric_signal: Dict[str, Any],
        trace_signal: Dict[str, Any],
        change_signal: Dict[str, Any],
        context: SkillContext,
    ) -> List[Dict[str, Any]]:
        dominant = log_signal.get("dominant")
        candidates: List[Dict[str, Any]] = []
        if dominant:
            pattern = next(p for p in _FAILURE_PATTERNS if p["category"] == dominant)
            evidences: List[Dict[str, Any]] = []

            # 证据 1：日志
            matched_logs = log_signal["matches"].get(dominant, [])
            evidences.append(self._make_evidence(
                "logs",
                "strong" if len(matched_logs) >= 3 else ("weak" if matched_logs else "missing"),
                f"命中 {dominant} 模式的异常日志 {len(matched_logs)} 条",
                matched_logs[:3],
            ))

            # 证据 2：指标（异常指标名命中模式提示词才算强证据）
            anomalies = metric_signal["anomalies"]
            hint_hits = [
                a for a in anomalies
                if any(h in str(a["name"]).lower() for h in pattern["metric_hint"])
            ]
            evidences.append(self._make_evidence(
                "metrics",
                "strong" if hint_hits else ("weak" if anomalies else "missing"),
                f"检出 {len(anomalies)} 项异常指标，其中 {len(hint_hits)} 项与该模式直接相关",
                [f"{a['name']}({a['service']}): {a['baseline']} -> {a['current']} {a['unit']}" for a in (hint_hits or anomalies)[:3]],
            ))

            # 证据 3：链路（受影响服务上存在错误/慢 span 为强证据）
            bottleneck_spans = [
                s for s in trace_signal["error_spans"] + trace_signal["slow_spans"]
                if s["service"] in services
            ]
            evidences.append(self._make_evidence(
                "traces",
                "strong" if bottleneck_spans else (
                    "weak" if (trace_signal["error_spans"] or trace_signal["slow_spans"]) else "missing"
                ),
                f"受影响服务上定位到 {len(bottleneck_spans)} 个错误/慢 span",
                [f"{s['service']}.{s['operation']}: {s['duration_ms']}ms [{s['status']}]" for s in bottleneck_spans[:3]],
            ))

            # 证据 4：变更关联（服务相关变更 > 网络类变更 > 时间窗内其他变更）
            related_change = None
            if change_signal["related"]:
                related_change = change_signal["related"][0]
                change_strength = "strong"
            elif dominant == "network_latency" and change_signal["network_changes"]:
                related_change = change_signal["network_changes"][0]
                change_strength = "strong"
            elif change_signal["in_window"]:
                related_change = change_signal["in_window"][0]
                change_strength = "weak"
            else:
                change_strength = "missing"
            evidences.append(self._make_evidence(
                "changes",
                change_strength,
                (
                    f"告警前 {_CHANGE_WINDOW_HOURS}h 内存在关联变更 {related_change.get('change_id')}"
                    f"（{related_change.get('title')}，发布单 {related_change.get('release_ticket', 'N/A')}）"
                    if related_change else f"告警前 {_CHANGE_WINDOW_HOURS}h 内无变更记录"
                ),
                [
                    f"{related_change.get('change_id')} @ {related_change.get('end_time')} by {related_change.get('submitted_by', 'unknown')}"
                ] if related_change else [],
            ))

            # 置信度 = 基线 + 强证据加权 + 弱证据加权
            strong_count = sum(1 for e in evidences if e["strength"] == "strong")
            weak_count = sum(1 for e in evidences if e["strength"] == "weak")
            confidence = min(0.95, round(0.35 + 0.15 * strong_count + 0.05 * weak_count, 2))

            # 生成假设文本：主嫌疑服务取日志命中最多的受影响服务（避免字母序误选网关）
            hits = log_signal.get("service_hits", {}).get(dominant, {})
            in_scope = {svc: cnt for svc, cnt in hits.items() if svc in services} or hits
            primary_service = max(in_scope, key=in_scope.get, default=services[0] if services else "unknown")
            templates = _HYPOTHESIS_TEMPLATES[dominant]
            if related_change and change_strength == "strong":
                hypothesis = templates["with_change"].format(
                    service=primary_service,
                    change_id=related_change.get("change_id"),
                    change_title=related_change.get("title"),
                )
            else:
                hypothesis = templates["no_change"].format(service=primary_service)

            candidates.append({
                "category": dominant,
                "hypothesis": hypothesis,
                "service": primary_service,
                "confidence": confidence,
                "evidences": evidences,
                "related_change_id": related_change.get("change_id") if (related_change and change_strength == "strong") else None,
            })

        # 兜底备选假设：保持候选列表 >= 2，便于展示排序与置信度对比
        candidates.append({
            "category": "generic_dependency",
            "hypothesis": "下游依赖抖动引发的连锁反应（低置信度备选假设，主假设不成立时排查）",
            "service": services[0] if services else "unknown",
            "confidence": 0.15,
            "evidences": [
                self._make_evidence("logs", "weak", "异常日志与主假设模式高度重合，独立信号不足", []),
                self._make_evidence("metrics", "missing", "无独立指向依赖抖动的指标信号", []),
                self._make_evidence("traces", "missing", "无独立指向依赖抖动的链路信号", []),
                self._make_evidence("changes", "missing", "无独立指向依赖抖动的变更记录", []),
            ],
            "related_change_id": None,
        })
        candidates.sort(key=lambda c: c["confidence"], reverse=True)
        context.logger.info(
            f"    根因候选: {len(candidates)} 个，Top1 置信度 {candidates[0]['confidence']}",
            top1=candidates[0]["hypothesis"][:60],
        )
        return candidates

    @staticmethod
    def _make_evidence(source: str, strength: str, description: str, details: List[str]) -> Dict[str, Any]:
        return {"source": source, "strength": strength, "description": description, "details": details}
