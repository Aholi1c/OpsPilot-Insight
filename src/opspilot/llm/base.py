# -*- coding: utf-8 -*-
"""LLM Provider 抽象：所有 Agent 通过统一接口调用大模型，便于替换与测试。

默认使用 MockProvider（基于规则/模板的确定性输出），保证无 API Key、
无网络环境下完整跑通；生产环境可通过环境变量切换 DashScopeProvider。
"""
from __future__ import annotations

import os
from abc import ABC, abstractmethod
from typing import Optional


class LLMProvider(ABC):
    """LLM 提供方抽象基类。"""

    provider_name: str = "base"

    @abstractmethod
    def complete(self, prompt: str, **kwargs) -> str:
        """给定 prompt 返回补全文本。"""
        raise NotImplementedError


def create_provider(name: Optional[str] = None) -> LLMProvider:
    """Provider 工厂。

    优先级：显式入参 > 环境变量 OPSPILOT_LLM_PROVIDER > 默认 mock。
    """
    # 延迟导入避免循环依赖
    from .dashscope_provider import DashScopeProvider
    from .mock_provider import MockProvider

    name = (name or os.environ.get("OPSPILOT_LLM_PROVIDER", "mock")).lower()
    if name == "mock":
        return MockProvider()
    if name == "dashscope":
        return DashScopeProvider()
    raise ValueError(f"未知的 LLM Provider: {name}（可选：mock / dashscope）")
