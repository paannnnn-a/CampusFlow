"""向量索引层：优先 Chroma 持久化，缺依赖时回退到纯内存实现。

为什么要有回退：
Chroma 不是 B 的强依赖项——没有它测试仍然要能跑、同事 clone 下来也要能启动。
因此把「存向量 + 相似度检索」抽成 ``VectorIndex`` 接口：
- ``ChromaIndex``：正式使用的持久化实现，集合名带 embedder 指纹，避免换模型后误用旧向量。
- ``MemoryIndex``：零依赖的余弦检索，用于单元测试与无 chromadb 的环境。

两种实现的检索语义一致（余弦相似度、按相似度降序），因此上层 retriever 不需要分支。
"""
from __future__ import annotations

import math
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from .ingest import Chunk


@dataclass(frozen=True)
class Hit:
    """一条检索命中，保留足够信息做引用展示。"""

    chunk_id: str
    source_id: str
    title: str
    content: str
    score: float
    metadata: Dict[str, object]

    @property
    def source_type(self) -> str:
        return str(self.metadata.get("source_type", "simulated"))

    @property
    def url(self) -> str:
        return str(self.metadata.get("url", ""))

    @property
    def page(self) -> Optional[int]:
        raw = self.metadata.get("page")
        return int(raw) if isinstance(raw, (int, float)) and int(raw) > 0 else None


class VectorIndex(ABC):
    """向量索引接口。写入与查询都以 chunk 为单位。"""

    @abstractmethod
    def add(self, chunks: Sequence[Chunk], vectors: Sequence[Sequence[float]]) -> int:
        """写入或覆盖 chunk，返回实际写入条数。"""

    @abstractmethod
    def search(self, vector: Sequence[float], limit: int) -> List[Hit]:
        """按余弦相似度返回前 limit 条。"""

    @abstractmethod
    def count(self) -> int:
        """索引内条目总数。"""

    def reset(self) -> None:
        """清空索引，用于重建。"""


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if len(a) != len(b):
        raise ValueError("向量维度不一致，索引可能来自不同的 Embedder，请重新 ingest")
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


class MemoryIndex(VectorIndex):
    """进程内索引。重启即丢失，仅用于测试与无 Chroma 的降级运行。"""

    def __init__(self):
        self._items: Dict[str, tuple] = {}

    def add(self, chunks: Sequence[Chunk], vectors: Sequence[Sequence[float]]) -> int:
        if len(chunks) != len(vectors):
            raise ValueError("chunk 数与向量数不一致")
        for chunk, vector in zip(chunks, vectors):
            self._items[chunk.chunk_id] = (chunk, list(vector))
        return len(chunks)

    def search(self, vector: Sequence[float], limit: int) -> List[Hit]:
        scored = []
        for chunk, stored in self._items.values():
            scored.append(Hit(
                chunk_id=chunk.chunk_id,
                source_id=chunk.source_id,
                title=chunk.citation_title,
                content=chunk.content,
                score=_cosine(vector, stored),
                metadata=chunk.to_metadata(),
            ))
        # 分数相同时用 chunk_id 排序，保证结果可复现。
        scored.sort(key=lambda hit: (-hit.score, hit.chunk_id))
        return scored[:limit]

    def count(self) -> int:
        return len(self._items)

    def reset(self) -> None:
        self._items.clear()


class ChromaIndex(VectorIndex):
    """Chroma 持久化索引。

    集合名内嵌 embedder 指纹：更换 Embedding 方案后会写入新集合，
    旧向量不会被误用；旧集合需要手动删除 ``data/chroma_db`` 清理。
    """

    def __init__(self, persist_dir: Path, collection: str = "campus_knowledge"):
        try:
            import chromadb
        except ImportError as exc:
            raise ImportError(
                "未安装 chromadb。请执行 pip install chromadb，"
                "或改用 MemoryIndex（RAG_VECTOR_STORE=memory）。"
            ) from exc

        persist_dir = Path(persist_dir)
        persist_dir.mkdir(parents=True, exist_ok=True)
        self._client = chromadb.PersistentClient(path=str(persist_dir))
        self._name = re.sub(r"[^A-Za-z0-9_.-]", "_", collection)[:63] or "campus_knowledge"
        # 显式提供 embedding_function=None：向量由我们自己算，避免 Chroma 下载默认模型。
        self._collection = self._client.get_or_create_collection(
            name=self._name,
            embedding_function=None,
            metadata={"hnsw:space": "cosine"},
        )

    def add(self, chunks: Sequence[Chunk], vectors: Sequence[Sequence[float]]) -> int:
        if not chunks:
            return 0
        if len(chunks) != len(vectors):
            raise ValueError("chunk 数与向量数不一致")
        self._collection.upsert(
            ids=[c.chunk_id for c in chunks],
            embeddings=[list(v) for v in vectors],
            documents=[c.content for c in chunks],
            metadatas=[c.to_metadata() for c in chunks],
        )
        return len(chunks)

    def search(self, vector: Sequence[float], limit: int) -> List[Hit]:
        if self.count() == 0:
            return []
        result = self._collection.query(
            query_embeddings=[list(vector)],
            n_results=max(1, limit),
            include=["documents", "metadatas", "distances"],
        )
        ids = (result.get("ids") or [[]])[0]
        documents = (result.get("documents") or [[]])[0]
        metadatas = (result.get("metadatas") or [[]])[0]
        distances = (result.get("distances") or [[]])[0]

        hits: List[Hit] = []
        for index, chunk_id in enumerate(ids):
            metadata = dict(metadatas[index] or {})
            # cosine 空间下 distance = 1 - cosine_similarity。
            distance = distances[index] if index < len(distances) else 1.0
            hits.append(Hit(
                chunk_id=chunk_id,
                source_id=str(metadata.get("source_id", "")),
                title=str(metadata.get("title", "")),
                content=documents[index] if index < len(documents) else "",
                score=round(1.0 - float(distance), 6),
                metadata=metadata,
            ))
        hits.sort(key=lambda hit: (-hit.score, hit.chunk_id))
        return hits[:limit]

    def count(self) -> int:
        return int(self._collection.count())

    def reset(self) -> None:
        self._client.delete_collection(self._name)
        self._collection = self._client.get_or_create_collection(
            name=self._name, embedding_function=None, metadata={"hnsw:space": "cosine"}
        )


def build_index(persist_dir: Path, collection: str, *, prefer_chroma: bool = True) -> VectorIndex:
    """构造索引：优先 Chroma，不可用时静默回退到内存实现。"""
    if prefer_chroma:
        try:
            return ChromaIndex(persist_dir, collection=collection)
        except ImportError:
            pass
    return MemoryIndex()
