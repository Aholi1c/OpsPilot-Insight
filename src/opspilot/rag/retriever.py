# -*- coding: utf-8 -*-
"""RAG 检索器：可插拔后端。

- LocalRetriever（默认）：纯 Python 分词 + BM25 相似度，零额外依赖，离线可用；
- ChromaRetriever（可选）：chromadb 向量库 + DashScope embedding，
  安装 requirements-optional.txt 并设置 OPSPILOT_RAG_BACKEND=chroma 时启用。

分词策略（LocalRetriever）：ASCII 词 + 中文单字 + 中文二元组（bigram），
对中文运维文本（如"连接池耗尽"）无需词典即可获得可用的召回效果。
"""
from __future__ import annotations

import math
import os
import re
from abc import ABC, abstractmethod
from collections import Counter
from typing import Any, Dict, List, Optional

from .store import KnowledgeStore

# BM25 超参（经验默认值）
_BM25_K1 = 1.5
_BM25_B = 0.75

_ASCII_TOKEN = re.compile(r"[a-z0-9_\-./]+")
_CJK_CHAR = re.compile(r"[\u4e00-\u9fff]")


def tokenize(text: str) -> List[str]:
    """混合分词：ASCII 词 + 中文单字 + 中文 bigram。"""
    text = text.lower()
    tokens = _ASCII_TOKEN.findall(text)
    cjk_chars = _CJK_CHAR.findall(text)
    tokens.extend(cjk_chars)
    # 相邻中文字符组成 bigram（按原文顺序，跨非中文字符断开）
    run: List[str] = []
    for ch in text:
        if _CJK_CHAR.match(ch):
            run.append(ch)
        else:
            tokens.extend(a + b for a, b in zip(run, run[1:]))
            run = []
    tokens.extend(a + b for a, b in zip(run, run[1:]))
    return tokens


class BaseRetriever(ABC):
    """检索器抽象：search() 返回 [{doc, score}]，按相关度降序。"""

    backend_name = "base"

    def __init__(self, store: KnowledgeStore):
        self.store = store

    @abstractmethod
    def search(
        self, query: str, doc_type: Optional[str] = None, top_k: int = 3,
    ) -> List[Dict[str, Any]]:
        raise NotImplementedError


class LocalRetriever(BaseRetriever):
    """纯 Python BM25 检索（默认后端，离线零依赖）。"""

    backend_name = "local"

    def search(
        self, query: str, doc_type: Optional[str] = None, top_k: int = 3,
    ) -> List[Dict[str, Any]]:
        docs = self.store.all_docs(doc_type)
        if not docs:
            return []
        corpus = [tokenize(self.store.doc_text(doc)) for doc in docs]
        query_tokens = tokenize(query)
        scores = self._bm25(corpus, query_tokens)
        ranked = sorted(zip(docs, scores), key=lambda x: x[1], reverse=True)
        return [
            {"doc": doc, "score": round(score, 4)}
            for doc, score in ranked[:top_k] if score > 0
        ]

    @staticmethod
    def _bm25(corpus: List[List[str]], query_tokens: List[str]) -> List[float]:
        """标准 BM25：对每篇文档计算查询词累计得分。"""
        doc_count = len(corpus)
        avg_len = sum(len(d) for d in corpus) / doc_count if doc_count else 0.0
        # 文档频率
        doc_freq: Counter = Counter()
        for doc in corpus:
            for token in set(doc):
                doc_freq[token] += 1
        scores = []
        for doc in corpus:
            tf = Counter(doc)
            score = 0.0
            for token in query_tokens:
                if token not in tf:
                    continue
                idf = math.log(1 + (doc_count - doc_freq[token] + 0.5) / (doc_freq[token] + 0.5))
                freq = tf[token]
                denom = freq + _BM25_K1 * (1 - _BM25_B + _BM25_B * len(doc) / (avg_len or 1))
                score += idf * freq * (_BM25_K1 + 1) / denom
            scores.append(score)
        return scores


class ChromaRetriever(BaseRetriever):
    """chromadb + DashScope embedding 检索（可选后端）。

    依赖：pip install -r requirements-optional.txt；
    环境：OPSPILOT_RAG_BACKEND=chroma、DASHSCOPE_API_KEY=sk-xxx。
    默认不安装 chromadb 不影响主流程（工厂函数会回退到 LocalRetriever）。
    """

    backend_name = "chroma"
    _EMBED_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/embeddings"
    _EMBED_MODEL = "text-embedding-v3"

    def __init__(self, store: KnowledgeStore):
        super().__init__(store)
        import chromadb  # 延迟导入：未安装时由 create_retriever 捕获

        self.api_key = os.environ.get("DASHSCOPE_API_KEY", "")
        if not self.api_key:
            raise RuntimeError("ChromaRetriever 需要环境变量 DASHSCOPE_API_KEY 提供 embedding 能力")
        self._client = chromadb.Client()
        self._collection = self._client.get_or_create_collection("opspilot_knowledge")
        self._index_docs()

    def _embed(self, texts: List[str]) -> List[List[float]]:
        """调用 DashScope embedding（OpenAI 兼容模式，标准库 urllib 实现）。"""
        import json as _json
        import urllib.request

        request = urllib.request.Request(
            self._EMBED_URL,
            data=_json.dumps({"model": self._EMBED_MODEL, "input": texts}).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.api_key}"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=30) as resp:
            payload = _json.loads(resp.read().decode("utf-8"))
        return [item["embedding"] for item in payload["data"]]

    def _index_docs(self) -> None:
        """全量文档构建向量索引（知识库规模小，直接重建）。"""
        docs = self.store.all_docs()
        if not docs:
            return
        texts = [self.store.doc_text(doc) for doc in docs]
        self._collection.upsert(
            ids=[str(doc.get("id", i)) for i, doc in enumerate(docs)],
            embeddings=self._embed(texts),
            metadatas=[{"type": doc.get("type", ""), "raw": __import__("json").dumps(doc, ensure_ascii=False)}
                       for doc in docs],
        )

    def search(
        self, query: str, doc_type: Optional[str] = None, top_k: int = 3,
    ) -> List[Dict[str, Any]]:
        import json as _json

        where = {"type": doc_type} if doc_type else None
        result = self._collection.query(
            query_embeddings=self._embed([query]), n_results=top_k, where=where,
        )
        hits = []
        for meta, distance in zip(result["metadatas"][0], result["distances"][0]):
            hits.append({"doc": _json.loads(meta["raw"]), "score": round(1 - distance, 4)})
        return hits


def create_retriever(store: KnowledgeStore, backend: Optional[str] = None) -> BaseRetriever:
    """检索器工厂：环境变量 OPSPILOT_RAG_BACKEND 选择后端（默认 local）。

    chroma 后端不可用（未安装/缺 Key）时回退 LocalRetriever，保证离线可跑。
    """
    backend = (backend or os.environ.get("OPSPILOT_RAG_BACKEND", "local")).lower()
    if backend == "chroma":
        try:
            return ChromaRetriever(store)
        except Exception:  # noqa: BLE001 —— 缺依赖/缺 Key 时回退本地检索
            return LocalRetriever(store)
    return LocalRetriever(store)
