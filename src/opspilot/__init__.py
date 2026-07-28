# -*- coding: utf-8 -*-
"""OpsPilot-Insight：零人工运维场景的多 Agent 自愈系统（阶段 1：最小闭环）。

流水线：告警接入(AlertAgent) -> 根因分析(RcaAgent) -> 修复规划(PlannerAgent)
内建可观测能力：自研轻量 Tracer（OpenTelemetry 语义）+ JSON lines 结构化日志。
"""

__version__ = "0.1.0"
