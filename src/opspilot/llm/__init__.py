# -*- coding: utf-8 -*-
"""LLM 调用层：Provider 抽象 + Mock（默认）/ DashScope（通义千问）实现。"""

from .base import LLMProvider, create_provider
from .dashscope_provider import DashScopeProvider
from .mock_provider import MockProvider

__all__ = ["LLMProvider", "MockProvider", "DashScopeProvider", "create_provider"]
