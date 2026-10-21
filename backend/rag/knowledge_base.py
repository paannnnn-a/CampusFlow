"""知识库编排：把解析、向量化、索引、检索串起来。

对外只暴露 ``KnowledgeBase`` 的三个动作：``build``（重建索引）、``search``（检索）、
``stats``（自检）。业务层的 ``search_campus_knowledge`` 只依赖 ``search``。

设计要点：
1. **可检索才可引用**：每个返回项都必须带 ``source_id`` 与 ``title``，否则用户无法核对依据。
2. **无依据不编造**：没有命中就返回空列表，由业务层给出「未查到」的明确答复。
3. **阈值可调**：特征哈希向量的相似度分布与语义模型不同，阈值通过参数注入，
   而不是写死在代码里，方便换 Embedder 后重新标定。
4. **索引指纹**：集合名含 embedder 指纹，换模型不会静默复用旧向量。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Mapping, Optional, Sequence

from .embedder import Embedder, EmbeddingError, build_embedder
from .hybrid import BM25Index, apply_section_boost, fuse
from .ingest import Chunk, DocumentError, load_directory
from .vector_store import Hit, VectorIndex, build_index

#: 低于该相似度视为不相关。哈希向量实测：相关片段通常 > 0.30，无关片段 < 0.15。
DEFAULT_MIN_SCORE = 0.18
#: 融合时向量一路的权重。语义 Embedding 可调高，纯词法场景可调低。
DEFAULT_VECTOR_WEIGHT = 0.5
#: 章节标题命中查询主题的加成权重，与 RRF 分量级对齐。
DEFAULT_SECTION_WEIGHT = 0.02
#: 词法一路的最低可信分数。BM25 无任何词元命中时为 0；
#: 只要有一两个 2-gram 命中就会显著高于 0，因此取一个正值即可挡住全零结果。
DEFAULT_MIN_LEXICAL_SCORE = 0.5
#: 词法一路的最低查询覆盖率（命中查询词元数 / 查询词元总数）。
#: 实测「量子力学实验报告」最高只覆盖 0.14（仅命中「报告」），
#: 而真实命中普遍 ≥ 0.4，故 0.3 能有效挡住「只撞上一个词」的误命中。
DEFAULT_MIN_COVERAGE = 0.3


class KnowledgeUnavailable(RuntimeError):
    """知识库不可用（索引为空、Embedding 失败等）。上层应转为 TOOL_UNAVAILABLE。"""


@dataclass(frozen=True)
class SearchOutcome:
    """检索结果，附带自检信息，方便 Agent 说明依据不足。"""

    hits: List[Hit]
    total_indexed: int
    min_score: float

    @property
    def found(self) -> bool:
        return bool(self.hits)


class KnowledgeBase:
    def __init__(self, *, embedder: Embedder, index: VectorIndex,
                 min_score: float = DEFAULT_MIN_SCORE,
                 vector_weight: float = DEFAULT_VECTOR_WEIGHT,
                 section_weight: float = DEFAULT_SECTION_WEIGHT,
                 min_lexical_score: float = DEFAULT_MIN_LEXICAL_SCORE,
                 min_coverage: float = DEFAULT_MIN_COVERAGE):
        self.embedder = embedder
        self.index = index
        self.min_score = min_score
        self.vector_weight = vector_weight
        self.section_weight = section_weight
        self.min_lexical_score = min_lexical_score
        self.min_coverage = min_coverage
        # 词法索引随向量索引一起重建，两者必须覆盖同一批 chunk。
        self._bm25 = BM25Index()

    # ---------------------------------------------------------------- 构建

    def build(self, chunks: Sequence[Chunk], *, reset: bool = True) -> int:
        """重建索引。向量化在写入前一次性完成，失败时索引保持原状。"""
        if reset:
            self.index.reset()
            self._bm25.reset()
        if not chunks:
            return 0

        texts = [chunk.content for chunk in chunks]
        try:
            vectors = self.embedder.embed_documents(texts)
        except EmbeddingError as exc:
            raise KnowledgeUnavailable(str(exc)) from exc

        if len(vectors) != len(chunks):
            raise KnowledgeUnavailable("Embedding 返回条数与片段数不一致")

        written = self.index.add(chunks, vectors)
        # 标题与章节名也参与词法匹配：用户常直接说事务名而不说正文用词。
        self._bm25.index(chunks, extra_text=[chunk.citation_title for chunk in chunks])
        return written

    def build_from_directory(self, directory: Path, *, reset: bool = True,
                             max_chars: int = 400, overlap: int = 60) -> int:
        chunks = load_directory(directory, max_chars=max_chars, overlap=overlap)
        return self.build(chunks, reset=reset)

    # ---------------------------------------------------------------- 检索

    def search(self, query: str, *, limit: int = 4,
               min_score: Optional[float] = None) -> SearchOutcome:
        """混合检索：向量召回 + BM25 召回，按排名融合。

        ``min_score`` 只作用于向量一路的候选过滤；纯词法精确命中
        （如查询「所需材料」直击标题）即使向量分很低也应保留，
        否则会丢掉最该给出的依据。
        """
        threshold = self.min_score if min_score is None else min_score
        query = (query or "").strip()
        total = self.index.count()
        if not query or total == 0:
            return SearchOutcome(hits=[], total_indexed=total, min_score=threshold)

        try:
            vector = self.embedder.embed_query(query)
        except EmbeddingError as exc:
            raise KnowledgeUnavailable(str(exc)) from exc

        # 两路都多取一些，给融合留出余量。
        pool = max(limit * 4, limit)
        vector_hits = [hit for hit in self.index.search(vector, pool) if hit.score >= threshold]
        lexical_hits = self._bm25.search(query, pool)

        # BM25 对任何查询都会返回排序结果，即使分数全为 0（没有任何词元命中）。
        # 必须显式过滤：词法一路只有在真正命中查询词时才可信，
        # 否则「量子力学实验报告」也会返回一篇学生证文档，属于错答。
        #
        # 两个条件缺一不可：
        # 1. 绝对分数下限——挡住完全没有词元命中的结果；
        # 2. 查询覆盖率下限——挡住「只撞上一个高频词」的结果。BM25 绝对分数
        #    会被文档长度与词频放大，误命中也能拿到 4 分以上，单靠分数卡不住。
        coverage = {}
        if self.min_coverage > 0:
            coverage = dict(zip(
                [c.chunk_id for c in self._bm25._chunks],
                self._bm25.coverage(query),
            ))
        lexical_hits = [
            (chunk, score) for chunk, score in lexical_hits
            if score >= self.min_lexical_score
            and coverage.get(chunk.chunk_id, 1.0) >= self.min_coverage
        ]

        if not vector_hits and not lexical_hits:
            return SearchOutcome(hits=[], total_indexed=total, min_score=threshold)
        if not self._bm25:
            # 降级：词法索引缺失时退化为纯向量检索。
            return SearchOutcome(hits=vector_hits[:limit], total_indexed=total,
                                 min_score=threshold)

        fused = fuse(vector_hits, lexical_hits, limit=max(pool, limit),
                     alpha=self.vector_weight)
        fused = apply_section_boost(fused, query, weight=self.section_weight)[:limit]
        return SearchOutcome(hits=fused, total_indexed=total, min_score=threshold)

    def stats(self) -> dict:
        return {
            "indexed": self.index.count(),
            "lexical_indexed": len(self._bm25),
            "embedder": self.embedder.fingerprint(),
            "min_score": self.min_score,
            "vector_weight": self.vector_weight,
            "section_weight": self.section_weight,
            "index_type": type(self.index).__name__,
        }


def _env_int(env: Mapping[str, str], key: str, default: int) -> int:
    raw = (env.get(key) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ValueError("%s 必须是整数" % key)


def _env_float(env: Mapping[str, str], key: str, default: float) -> float:
    raw = (env.get(key) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        raise ValueError("%s 必须是数字" % key)


def collection_name(embedder: Embedder, base: str = "campus_knowledge") -> str:
    """集合名带 embedder 指纹，换模型自动落到新集合。"""
    fingerprint = embedder.fingerprint().replace(":", "-").replace(".", "_")
    raw = "%s__%s" % (base, fingerprint)
    safe = "".join(ch if ch.isalnum() or ch in "_-" else "_" for ch in raw)
    # Chroma 要求 3–63 个字符。
    if len(safe) < 3:
        safe = "kn_" + safe
    return safe[:63]


def build_knowledge_base(env: Optional[Mapping[str, str]] = None,
                         *, prefer_chroma: bool = True) -> KnowledgeBase:
    """按环境变量装配知识库。默认离线可用、缺 Chroma 时自动回退内存索引。"""
    source = os.environ if env is None else env
    embedder = build_embedder(source)

    store = (source.get("RAG_VECTOR_STORE") or "chroma").strip().lower()
    if store not in {"chroma", "memory"}:
        raise ValueError("RAG_VECTOR_STORE 仅支持 chroma 或 memory")
    use_chroma = prefer_chroma and store == "chroma"

    persist_dir = Path(source.get("RAG_CHROMA_DIR") or "data/chroma_db")
    index = build_index(persist_dir, collection_name(embedder), prefer_chroma=use_chroma)

    # 检索参数都从环境读取，换 Embedding 方案后不必改代码即可重新标定。
    return KnowledgeBase(
        embedder=embedder,
        index=index,
        min_score=_env_float(source, "RAG_MIN_SCORE", DEFAULT_MIN_SCORE),
        min_coverage=_env_float(source, "RAG_MIN_COVERAGE", DEFAULT_MIN_COVERAGE),
    )
