"""RAG 检索层。

- ``ingest``：文档解析与切分（md / txt / pdf），带来源标注。
- ``embedder``：向量化抽象，默认离线实现，可切云端 API。
- ``vector_store``：向量索引，优先 Chroma，缺依赖回退内存。
- ``knowledge_base``：编排入口与检索阈值控制。
"""
from .embedder import Embedder, EmbeddingError, HashingEmbedder, build_embedder
from .ingest import Chunk, DocumentError, DocumentMeta, load_directory, load_document
from .knowledge_base import (
    DEFAULT_MIN_SCORE,
    KnowledgeBase,
    KnowledgeUnavailable,
    SearchOutcome,
    build_knowledge_base,
)
from .vector_store import ChromaIndex, Hit, MemoryIndex, VectorIndex

__all__ = [
    "Chunk",
    "ChromaIndex",
    "DEFAULT_MIN_SCORE",
    "DocumentError",
    "DocumentMeta",
    "Embedder",
    "EmbeddingError",
    "HashingEmbedder",
    "Hit",
    "KnowledgeBase",
    "KnowledgeUnavailable",
    "MemoryIndex",
    "SearchOutcome",
    "VectorIndex",
    "build_embedder",
    "build_knowledge_base",
    "load_directory",
    "load_document",
]
