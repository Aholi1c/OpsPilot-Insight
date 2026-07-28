# -*- coding: utf-8 -*-
"""Skill 基类：统一元数据（name/version/schema/preconditions/failure_policy），
execute() 自动包 Trace Span 并产出标准化 SkillResult。

失败策略（failure_policy）：
- degrade：捕获异常，返回 success=False 的 SkillResult，由调用方决定降级路径；
- abort  ：异常原样上抛，中断当前 Agent 流程（由 Orchestrator 兜底）。
"""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..models import SkillResult
from ..observability import AuditLog, JsonLogger, MetricsCollector, Tracer


@dataclass
class SkillContext:
    """Skill 执行上下文：链路、日志、MCP 适配器与场景信息。

    阶段 2 扩展：审计事件流（audit）、进程内指标（metrics）与额外依赖
    （extras：动作白名单 / RAG 检索器 / 知识库 / 审批回调等）。
    """

    tracer: Tracer
    logger: JsonLogger
    adapters: Dict[str, Any] = field(default_factory=dict)
    scenario: str = ""
    audit: Optional[AuditLog] = None
    metrics: Optional[MetricsCollector] = None
    extras: Dict[str, Any] = field(default_factory=dict)


class Skill(ABC):
    """Skill 抽象基类。子类只需实现 run()，Trace/计时/异常处理由基类负责。"""

    name: str = "base_skill"
    version: str = "0.1.0"
    description: str = ""
    input_schema: Dict[str, str] = {}   # 参数名 -> 说明（轻量 schema，阶段 2 换 JSON Schema）
    output_schema: Dict[str, str] = {}
    preconditions: List[str] = []       # 执行前置条件（payload 必须包含的键）
    failure_policy: str = "degrade"     # degrade / abort

    def execute(self, payload: Dict[str, Any], context: SkillContext) -> SkillResult:
        """统一执行入口：前置校验 -> run() -> 封装 SkillResult，全程带 Span。"""
        with context.tracer.start_span(
            f"skill.{self.name}",
            kind="INTERNAL",
            attributes={"skill.name": self.name, "skill.version": self.version},
        ) as span:
            context.logger.info(f"  ⚙ Skill [{self.name}] 开始执行", skill=self.name)
            started = time.perf_counter()
            try:
                self._check_preconditions(payload)
                output = self.run(payload, context)
                duration_ms = (time.perf_counter() - started) * 1000
                span.set_attribute("skill.success", True)
                if context.metrics:
                    context.metrics.record_skill(self.name, True, duration_ms)
                context.logger.info(
                    f"  ⚙ Skill [{self.name}] 执行成功", skill=self.name,
                    duration_ms=round(duration_ms, 2),
                )
                return SkillResult(
                    skill_name=self.name, skill_version=self.version,
                    success=True, output=output, duration_ms=round(duration_ms, 2),
                )
            except Exception as exc:  # noqa: BLE001 —— 按失败策略处理
                duration_ms = (time.perf_counter() - started) * 1000
                span.status = "ERROR"
                span.status_message = f"{type(exc).__name__}: {exc}"
                span.set_attribute("skill.success", False)
                if context.metrics:
                    context.metrics.record_skill(self.name, False, duration_ms)
                context.logger.error(
                    f"  ⚙ Skill [{self.name}] 执行失败: {exc}", skill=self.name,
                    failure_policy=self.failure_policy,
                )
                if self.failure_policy == "abort":
                    raise
                return SkillResult(
                    skill_name=self.name, skill_version=self.version,
                    success=False, error=str(exc), duration_ms=round(duration_ms, 2),
                )

    def _check_preconditions(self, payload: Dict[str, Any]) -> None:
        """校验前置条件：payload 缺少必需键时直接失败。"""
        missing = [key for key in self.preconditions if key not in payload]
        if missing:
            raise ValueError(f"Skill [{self.name}] 前置条件不满足，缺少入参: {missing}")

    @abstractmethod
    def run(self, payload: Dict[str, Any], context: SkillContext) -> Dict[str, Any]:
        """子类实现的业务逻辑，返回结构化输出。"""
        raise NotImplementedError
