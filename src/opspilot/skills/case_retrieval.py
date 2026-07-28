# -*- coding: utf-8 -*-
"""CaseRetrieval Skill：从知识库检索相似历史事故案例（RcaAgent / PlannerAgent 用）。

检索结果注入 Agent 的 LLM 上下文，用历史案例的"症状-根因-处置"经验
增强根因判断与方案制定；命中数记入 RAG 指标。
"""
from __future__ import annotations

from typing import Any, Dict, List

from .base import Skill, SkillContext


class CaseRetrievalSkill(Skill):
    name = "case_retrieval"
    version = "1.0.0"
    description = "历史案例检索：按事件症状检索相似历史事故，注入 Agent 决策上下文"
    input_schema = {
        "query": "检索查询（事件标题 + 症状 / 根因假设）",
        "top_k": "返回条数（默认 3）",
    }
    output_schema = {
        "cases": "命中案例列表 [{id/title/root_cause/resolution/score...}]",
        "backend": "实际使用的检索后端（local / chroma）",
    }
    preconditions = ["query"]
    failure_policy = "degrade"  # 检索失败不阻断主流程（RAG 为增强项）

    def run(self, payload: Dict[str, Any], context: SkillContext) -> Dict[str, Any]:
        retriever = context.extras.get("retriever")
        if retriever is None:
            raise RuntimeError("SkillContext.extras 缺少 retriever（RAG 检索器未装配）")
        top_k = int(payload.get("top_k", 3))
        hits = retriever.search(payload["query"], doc_type="case", top_k=top_k)
        if context.metrics:
            context.metrics.record_rag_query(len(hits))

        cases: List[Dict[str, Any]] = []
        for hit in hits:
            doc = hit["doc"]
            cases.append({
                "id": doc.get("id", ""),
                "title": doc.get("title", ""),
                "category": doc.get("category", ""),
                "symptoms": doc.get("symptoms", ""),
                "root_cause": doc.get("root_cause", ""),
                "resolution": doc.get("resolution", ""),
                "duration_minutes": doc.get("duration_minutes", 0),
                "score": hit["score"],
            })
        context.logger.info(
            f"    ⌕ 历史案例检索: 命中 {len(cases)} 条"
            + (f"，Top1={cases[0]['id']}（score={cases[0]['score']}）" if cases else "")
        )
        return {"cases": cases, "backend": retriever.backend_name}
