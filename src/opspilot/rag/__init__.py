# -*- coding: utf-8 -*-
"""RAG 层：知识库存储 + 可插拔检索后端（默认 LocalRetriever，离线零依赖）。"""

from .retriever import BaseRetriever, ChromaRetriever, LocalRetriever, create_retriever
from .store import KnowledgeStore

__all__ = [
    "KnowledgeStore",
    "BaseRetriever", "LocalRetriever", "ChromaRetriever", "create_retriever",
]
