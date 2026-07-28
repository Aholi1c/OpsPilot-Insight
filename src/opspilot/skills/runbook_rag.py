# -*- coding: utf-8 -*-
"""RunbookRag Skill：从知识库检索相关 Runbook 条目（PlannerAgent 用）。

修复方案必须引用 Runbook 依据（plan.runbook_references），让自动生成的
方案有"标准作业程序"背书，而非凭空生成；命中数记入 RAG 指标。
"""
from __future__ import annotations

from typing import Any, Dict, List

from .base import Skill, SkillContext


class RunbookRagSkill(Skill):
    name = "runbook_rag"
    version = "1.0.0"
    description = "Runbook 检索：按根因/症状检索标准运维手册条目，作为修复方案的依据引用"
    input_schema = {
        "query": "检索查询（根因假设 + 事件症状）",
        "top_k": "返回条数（默认 3）",
    }
    output_schema = {
        "runbooks": "命中 Runbook 列表 [{id/title/steps/score...}]",
        "backend": "实际使用的检索后端（local / chroma）",
    }
    preconditions = ["query"]
    failure_policy = "degrade"  # 检索失败时方案不带 Runbook 引用，不阻断主流程

    def run(self, payload: Dict[str, Any], context: SkillContext) -> Dict[str, Any]:
        retriever = context.extras.get("retriever")
        if retriever is None:
            raise RuntimeError("SkillContext.extras 缺少 retriever（RAG 检索器未装配）")
        top_k = int(payload.get("top_k", 3))
        hits = retriever.search(payload["query"], doc_type="runbook", top_k=top_k)
        if context.metrics:
            context.metrics.record_rag_query(len(hits))

        runbooks: List[Dict[str, Any]] = []
        for hit in hits:
            doc = hit["doc"]
            runbooks.append({
                "id": doc.get("id", ""),
                "title": doc.get("title", ""),
                "category": doc.get("category", ""),
                "steps": doc.get("steps", []),
                "score": hit["score"],
            })
        context.logger.info(
            f"    ⌕ Runbook 检索: 命中 {len(runbooks)} 条"
            + (f"，Top1={runbooks[0]['id']}（score={runbooks[0]['score']}）" if runbooks else "")
        )
        return {"runbooks": runbooks, "backend": retriever.backend_name}
