# -*- coding: utf-8 -*-
"""LLM-as-Judge：对根因分析质量 / 方案合理性 / 复盘质量三个维度打 1-5 分并给评语。

- MockJudge（默认）：基于规则评估特征的确定性评分与评语，离线可复现；
- DashScopeJudge：复用现有 LLM Provider 抽象（DashScopeProvider），提示词要求
  输出严格 JSON，解析失败自动回退 MockJudge 结果——代码就绪，设好 Key 即可启用：
  export OPSPILOT_JUDGE=dashscope && export DASHSCOPE_API_KEY=sk-xxx
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional

from ..llm.base import LLMProvider, create_provider

# 评审维度：(键, 展示名, 关联的规则项)
JUDGE_DIMENSIONS = [
    ("root_cause_quality", "根因分析质量", "root_cause"),
    ("plan_soundness", "方案合理性", "action_type"),
    ("postmortem_quality", "复盘质量", "verification"),
]


class BaseJudge:
    """Judge 抽象：judge(sample, rule_result) -> 维度评分列表。"""

    judge_name = "base"

    def judge(self, sample: Dict[str, Any], rule_result: Dict[str, Any]) -> List[Dict[str, Any]]:
        raise NotImplementedError


class MockJudge(BaseJudge):
    """确定性 Judge：由规则评估特征映射为 1-5 分与评语（离线默认）。"""

    judge_name = "mock"

    def judge(self, sample: Dict[str, Any], rule_result: Dict[str, Any]) -> List[Dict[str, Any]]:
        actual = sample.get("actual", {})
        rules = rule_result.get("rules", {})
        results = []
        for key, label, rule_key in JUDGE_DIMENSIONS:
            rule_score = (rules.get(rule_key) or {}).get("score", 0)
            score = self._to_five_scale(rule_score)
            results.append({
                "dimension": key, "label": label, "score": score,
                "comment": self._comment(key, score, actual),
            })
        return results

    @staticmethod
    def _to_five_scale(score_100: float) -> int:
        """0-100 规则分映射到 1-5 分。"""
        return max(1, min(5, 1 + int(score_100 / 25)))

    @staticmethod
    def _comment(dimension: str, score: int, actual: Dict[str, Any]) -> str:
        if dimension == "root_cause_quality":
            base = (f"根因假设「{actual.get('root_cause_hypothesis', '')[:40]}…」"
                    f"置信度 {actual.get('confidence')}，"
                    f"变更单关联{'明确' if actual.get('related_change_id') else '缺失'}")
            verdict = "证据链与变更时间线相互印证，结论可信。" if score >= 4 else \
                "关键要素命中不全，建议人工复核根因结论。"
        elif dimension == "plan_soundness":
            base = (f"执行动作 {len(actual.get('action_types', []))} 个，"
                    f"风险等级 {actual.get('risk_level') or '未知'}，"
                    f"执行状态 {actual.get('execution_status') or '未执行'}")
            verdict = "止血-缓解-恢复的动作编排合理，含回滚保障。" if score >= 4 else \
                "动作类型与预期不符或缺失回滚保障，方案需改进。"
        else:  # postmortem_quality
            base = (f"验证{'通过' if actual.get('verification_passed') else '未通过'}，"
                    f"复盘{'已生成' if actual.get('postmortem_present') else '缺失'}")
            verdict = "验证结论与复盘改进项完整，闭环质量良好。" if score >= 4 else \
                "验证或复盘段落不完整，闭环质量有缺口。"
        return f"{base}。{verdict}"


class DashScopeJudge(BaseJudge):
    """真实 LLM Judge：提示词要求输出 JSON，解析失败回退 MockJudge。"""

    judge_name = "dashscope"

    _PROMPT = (
        "你是运维多 Agent 系统的评测专家。请针对以下事故处理样本，"
        "从三个维度各打 1-5 分（5 为最佳）并各给一句中文评语。\n"
        "维度：root_cause_quality（根因分析质量）、plan_soundness（方案合理性）、"
        "postmortem_quality（复盘质量）。\n"
        "只输出严格 JSON 数组，元素形如 "
        '{{"dimension": "...", "score": 1-5, "comment": "..."}}，不要输出其它内容。\n'
        "样本：\n{context}"
    )

    def __init__(self, provider: Optional[LLMProvider] = None):
        self.provider = provider or create_provider("dashscope")
        self._fallback = MockJudge()

    def judge(self, sample: Dict[str, Any], rule_result: Dict[str, Any]) -> List[Dict[str, Any]]:
        context = json.dumps(
            {"input": sample.get("input"), "expected": sample.get("expected"),
             "actual": sample.get("actual")}, ensure_ascii=False,
        )
        try:
            completion = self.provider.complete(self._PROMPT.format(context=context))
            return self._parse(completion)
        except Exception:  # noqa: BLE001 —— 网络/解析失败均回退确定性 Judge
            return self._fallback.judge(sample, rule_result)

    @staticmethod
    def _parse(completion: str) -> List[Dict[str, Any]]:
        match = re.search(r"\[.*\]", completion, re.DOTALL)
        if not match:
            raise ValueError(f"Judge 输出不含 JSON 数组: {completion[:100]}")
        items = json.loads(match.group(0))
        labels = {key: label for key, label, _ in JUDGE_DIMENSIONS}
        results = []
        for item in items:
            dim = item.get("dimension", "")
            if dim not in labels:
                continue
            results.append({
                "dimension": dim, "label": labels[dim],
                "score": max(1, min(5, int(item.get("score", 3)))),
                "comment": str(item.get("comment", "")),
            })
        if len(results) != len(JUDGE_DIMENSIONS):
            raise ValueError("Judge 输出维度不完整")
        return results


def create_judge(name: Optional[str] = None) -> BaseJudge:
    """Judge 工厂：显式入参 > 环境变量 OPSPILOT_JUDGE > 默认 mock。"""
    name = (name or os.environ.get("OPSPILOT_JUDGE", "mock")).lower()
    if name == "mock":
        return MockJudge()
    if name == "dashscope":
        return DashScopeJudge()
    raise ValueError(f"未知的 Judge: {name}（可选：mock / dashscope）")
