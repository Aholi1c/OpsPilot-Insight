# -*- coding: utf-8 -*-
"""知识库存储：JSONL 文档加载 + 历史案例幂等沉淀。

数据目录 data/knowledge/：
- runbooks.jsonl：标准运维手册条目（type=runbook）
- cases.jsonl   ：历史事故案例（type=case），VerifierAgent 复盘后追加新案例
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Union


class KnowledgeStore:
    """基于 JSONL 文件的轻量知识库（零第三方依赖）。"""

    def __init__(self, knowledge_dir: Union[str, Path]):
        self.knowledge_dir = Path(knowledge_dir)
        self.runbooks_path = self.knowledge_dir / "runbooks.jsonl"
        self.cases_path = self.knowledge_dir / "cases.jsonl"

    @staticmethod
    def _load_jsonl(path: Path) -> List[Dict[str, Any]]:
        if not path.exists():
            return []
        docs = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                docs.append(json.loads(line))
        return docs

    def all_docs(self, doc_type: Optional[str] = None) -> List[Dict[str, Any]]:
        """返回全部文档（可按 type=runbook/case 过滤）。"""
        docs = self._load_jsonl(self.runbooks_path) + self._load_jsonl(self.cases_path)
        if doc_type:
            docs = [d for d in docs if d.get("type") == doc_type]
        return docs

    def append_case(self, case: Dict[str, Any]) -> bool:
        """追加一条历史案例（幂等：同 incident_id 不重复写入）。

        返回 True 表示实际写入，False 表示已存在跳过。
        """
        incident_id = case.get("incident_id", "")
        if incident_id:
            existing = {c.get("incident_id") for c in self._load_jsonl(self.cases_path)}
            if incident_id in existing:
                return False
        case = {"type": "case", **case}
        self.knowledge_dir.mkdir(parents=True, exist_ok=True)
        with self.cases_path.open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(case, ensure_ascii=False) + "\n")
        return True

    @staticmethod
    def doc_text(doc: Dict[str, Any]) -> str:
        """将文档拼接为检索用全文（title/keywords/症状/根因/步骤等）。"""
        parts: List[str] = []
        for key in ("title", "category", "symptoms", "root_cause", "resolution", "content"):
            value = doc.get(key, "")
            if isinstance(value, list):
                parts.extend(str(v) for v in value)
            elif value:
                parts.append(str(value))
        for key in ("keywords", "steps", "applicable_services", "services"):
            value = doc.get(key, [])
            if isinstance(value, list):
                parts.extend(str(v) for v in value)
            elif value:
                parts.append(str(value))
        return " ".join(parts)
