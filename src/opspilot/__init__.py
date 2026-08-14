# -*- coding: utf-8 -*-
"""OpsPilot-Insight：零人工运维场景的多 Agent 自愈系统。

流水线：告警接入(AlertAgent) -> 根因分析(RcaAgent) -> 修复规划(PlannerAgent)
        -> 安全执行(ExecutorAgent) -> 恢复验证与复盘(VerifierAgent)
内建可观测能力：自研轻量 Tracer（OpenTelemetry 语义）+ JSON lines 结构化日志。
"""

__version__ = "0.1.0"
