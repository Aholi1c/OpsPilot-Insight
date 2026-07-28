# -*- coding: utf-8 -*-
"""Agent 基类：统一身份定义、消息处理、LLM 调用与 Skill 调用，全部带 Trace。

身份字段（与 config/agents.yaml、docs/AGENT_IDENTITY.md 对齐）：
- name / role / capabilities / inputs / outputs / decision_boundary
"""
from __future__ import annotations

from abc import ABC, abstractmethod
import time
from typing import Any, Dict

from ..llm.base import LLMProvider
from ..models import AgentMessage
from ..observability import JsonLogger, Tracer
from ..observability.metrics import estimate_tokens
from ..skills.base import Skill, SkillContext


class BaseAgent(ABC):
    """Agent 抽象基类。子类实现 process() 完成各自职责。"""

    # 子类覆盖：与配置文件中 agents.<key> 对应
    config_key: str = ""

    def __init__(
        self,
        config: Dict[str, Any],
        llm: LLMProvider,
        tracer: Tracer,
        logger: JsonLogger,
        skills: Dict[str, Skill],
        skill_context: SkillContext,
    ):
        # ---- 身份定义（来自 config/agents.yaml）----
        self.name: str = config.get("name", self.__class__.__name__)
        self.role: str = config.get("role", "")
        self.capabilities = config.get("responsibilities", []) or []
        self.inputs = config.get("inputs", []) or []
        self.outputs = config.get("outputs", []) or []
        self.decision_boundary: str = config.get("decision_boundary", "")
        self.prompt_template: str = config.get("prompt_template", "[TASK=unknown]\n{context}")
        self.allowed_skills = set(config.get("skills", []) or [])
        # ---- 运行时依赖 ----
        self.llm = llm
        self.tracer = tracer
        self.logger = logger
        self.skills = skills
        self.skill_context = skill_context

    def handle(self, message: AgentMessage) -> AgentMessage:
        """处理入站消息：开 Agent 级 Span -> process() -> 封装出站消息。"""
        with self.tracer.start_span(
            f"agent.{self.name}",
            kind="INTERNAL",
            attributes={"agent.name": self.name, "agent.role": self.role,
                        "message.type": message.message_type, "message.sender": message.sender},
        ):
            self.logger.info(f"▶ Agent [{self.name}] 开始工作（角色: {self.role}）")
            started = time.perf_counter()
            content = self.process(message.content)
            if self.skill_context.metrics:
                self.skill_context.metrics.record_agent(
                    self.name, (time.perf_counter() - started) * 1000,
                )
            self.logger.info(f"■ Agent [{self.name}] 工作完成")
            return AgentMessage(
                sender=self.name,
                receiver=message.sender,
                message_type=self.output_message_type(),
                content=content,
                trace_id=self.tracer.trace_id,
            )

    def call_llm(self, context_text: str) -> str:
        """按提示词模板调用 LLM（带 CLIENT Span，记录 Provider/模型/token 与归因）。"""
        prompt = self.prompt_template.format(context=context_text)
        model = getattr(self.llm, "model", "") or self.llm.provider_name
        with self.tracer.start_span(
            "llm.complete",
            kind="CLIENT",
            attributes={"llm.provider": self.llm.provider_name, "llm.model": model,
                        "llm.prompt_chars": len(prompt)},
        ) as span:
            completion = self.llm.complete(prompt)
            span.set_attribute("llm.completion_chars", len(completion))
            # token 口径：DashScope 路径优先读 API 真实 usage，Mock 路径按字符数估算
            usage = getattr(self.llm, "last_usage", None) or {}
            prompt_tokens = usage.get("prompt_tokens") or estimate_tokens(prompt)
            completion_tokens = usage.get("completion_tokens") or estimate_tokens(completion)
            token_source = "api_usage" if usage else "estimated"
            span.set_attribute("llm.prompt_tokens", prompt_tokens)
            span.set_attribute("llm.completion_tokens", completion_tokens)
            if self.skill_context.metrics:
                self.skill_context.metrics.record_llm_call(
                    self.llm.provider_name, len(prompt), len(completion),
                    agent=self.name, model=model, skill=self._enclosing_skill(span),
                    prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
                    token_source=token_source,
                )
            return completion

    def _enclosing_skill(self, span) -> str:
        """沿父 span 链上溯，找到发起本次 LLM 调用的 Skill span（无则返回空串）。"""
        by_id = {s.span_id: s for s in self.tracer.spans}
        parent_id = span.parent_span_id
        while parent_id:
            parent = by_id.get(parent_id)
            if parent is None:
                break
            if parent.name.startswith("skill."):
                return parent.name[len("skill."):]
            parent_id = parent.parent_span_id
        return ""

    def call_skill(self, skill_name: str, payload: Dict[str, Any]):
        """调用 Skill（校验该 Agent 是否被授权使用此 Skill）。"""
        if self.allowed_skills and skill_name not in self.allowed_skills:
            raise PermissionError(f"Agent [{self.name}] 未被授权调用 Skill [{skill_name}]")
        skill = self.skills.get(skill_name)
        if skill is None:
            raise KeyError(f"Skill [{skill_name}] 未注册")
        return skill.execute(payload, self.skill_context)

    @abstractmethod
    def process(self, content: Dict[str, Any]) -> Dict[str, Any]:
        """子类实现：处理输入上下文，返回结构化输出。"""
        raise NotImplementedError

    @abstractmethod
    def output_message_type(self) -> str:
        """出站消息类型（用于消息路由与审计）。"""
        raise NotImplementedError
