# -*- coding: utf-8 -*-
"""DashScopeProvider：通义千问 Provider（OpenAI 兼容模式，标准库 urllib 实现）。

- 读取环境变量 DASHSCOPE_API_KEY，不引入 dashscope SDK；
- 阶段 1 不要求真实调用，代码路径完整可用，设置好 Key 即可切换：
  export OPSPILOT_LLM_PROVIDER=dashscope && export DASHSCOPE_API_KEY=sk-xxx
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from .base import LLMProvider

# DashScope OpenAI 兼容模式端点
_API_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"

_SYSTEM_PROMPT = (
    "你是 OpsPilot-Insight 多 Agent 自愈系统中的运维专家助手，"
    "输出必须简洁、专业、基于给定证据，不得编造事实。"
)


class DashScopeProvider(LLMProvider):
    """通义千问（DashScope）Provider。"""

    provider_name = "dashscope"

    def __init__(self, model: str = "qwen-plus", timeout: float = 30.0, temperature: float = 0.2):
        self.api_key = os.environ.get("DASHSCOPE_API_KEY", "")
        if not self.api_key:
            raise RuntimeError(
                "未检测到环境变量 DASHSCOPE_API_KEY；"
                "如需离线演示请使用默认的 MockProvider。"
            )
        self.model = model
        self.timeout = timeout
        self.temperature = temperature
        # 最近一次调用的真实 token 用量（prompt_tokens/completion_tokens，成本追踪读取）
        self.last_usage: dict = {}

    def complete(self, prompt: str, **kwargs) -> str:
        body = {
            "model": kwargs.get("model", self.model),
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "temperature": kwargs.get("temperature", self.temperature),
        }
        request = urllib.request.Request(
            _API_URL,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:  # 服务端返回错误码
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"DashScope API 调用失败（HTTP {exc.code}）: {detail}") from exc
        except urllib.error.URLError as exc:  # 网络不可达/超时
            raise RuntimeError(f"DashScope API 网络异常: {exc.reason}") from exc
        try:
            self.last_usage = payload.get("usage") or {}
            return payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as exc:
            raise RuntimeError(f"DashScope API 响应格式异常: {payload}") from exc
