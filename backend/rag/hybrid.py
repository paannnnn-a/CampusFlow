"""混合检索：向量相似度 + BM25 词法召回。

为什么需要混合
--------------
特征哈希向量擅长捕捉「整体像不像」，但对**短清单类片段**很不友好：
「所需材料」下只有「身份证 / 证件照 / 遗失说明」几个短条目，
向量相似度会被查询里的高频词（学生证、补办）稀释，导致材料清单排不到前面。

BM25 恰好相反：它按词频与逆文档频率打分，短而精准的命中会显著抬分。
两者融合后，既能保住向量的语义泛化，又能让关键词精确命中不丢。

正式切换到语义 Embedding（如 bge）后，BM25 仍有益处——专有名词、
部门名、表格编号这类精确匹配是语义模型的弱项。因此这层不是临时补丁。
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Dict, Iterable, List, Sequence

from .ingest import Chunk
from .vector_store import Hit

#: 检索用词元：中文按 2-gram 切（与 embedder 一致），英文按单词。
_WORD_RE = re.compile(r"[\u4e00-\u9fff]+|[A-Za-z]+|[0-9]+")
#: 常见虚词，作为中文 2-gram 时几乎无区分度，直接剔除可减少噪声。
_STOPWORDS = frozenset({
    "什么", "怎么", "如何", "哪些", "需要", "可以", "是否", "请问", "我要", "我想",
    "一下", "这个", "那个", "的是", "了的", "么办", "怎样", "多少", "有没有",
})


def tokenize(text: str) -> List[str]:
    """产出检索词元。中文用 2-gram，兼顾精确匹配与短语覆盖。"""
    tokens: List[str] = []
    for word in _WORD_RE.findall((text or "").lower()):
        if "\u4e00" <= word[0] <= "\u9fff":
            if len(word) == 1:
                tokens.append(word)
                continue
            for i in range(len(word) - 1):
                gram = word[i : i + 2]
                if gram not in _STOPWORDS:
                    tokens.append(gram)
        else:
            tokens.append(word)
    return tokens


class BM25Index:
    """轻量 BM25 实现，随 chunk 一起写入，不做持久化。

    ``k1`` 控制词频饱和，``b`` 控制长度归一化，取常用经验值即可。
    """

    def __init__(self, k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self._chunks: List[Chunk] = []
        self._term_freqs: List[Dict[str, int]] = []
        self._lengths: List[int] = []
        self._doc_freq: Dict[str, int] = {}
        self._avg_length: float = 0.0

    def __len__(self) -> int:
        return len(self._chunks)

    def reset(self) -> None:
        self._chunks = []
        self._term_freqs = []
        self._lengths = []
        self._doc_freq = {}
        self._avg_length = 0.0

    def index(self, chunks: Sequence[Chunk], *, extra_text=None) -> None:
        """建立索引。``extra_text`` 可为每个 chunk 追加标题等额外可检索文本。"""
        self.reset()
        for position, chunk in enumerate(chunks):
            text = chunk.content
            if extra_text is not None:
                text = "%s %s" % (extra_text[position], text)
            tokens = tokenize(text)
            freq: Dict[str, int] = {}
            for token in tokens:
                freq[token] = freq.get(token, 0) + 1
            self._chunks.append(chunk)
            self._term_freqs.append(freq)
            self._lengths.append(max(1, len(tokens)))
            for token in freq:
                self._doc_freq[token] = self._doc_freq.get(token, 0) + 1
        self._avg_length = (sum(self._lengths) / len(self._lengths)) if self._lengths else 0.0

    def scores(self, query: str) -> List[float]:
        """返回与 ``self._chunks`` 同序的 BM25 分数。"""
        if not self._chunks:
            return []
        query_terms = tokenize(query)
        if not query_terms:
            return [0.0] * len(self._chunks)

        total = len(self._chunks)
        results: List[float] = []
        for position in range(total):
            freq = self._term_freqs[position]
            length = self._lengths[position]
            score = 0.0
            for term in query_terms:
                tf = freq.get(term)
                if not tf:
                    continue
                df = self._doc_freq.get(term, 0)
                # 加 1 平滑，避免 df == total 时 IDF 归零或为负。
                idf = math.log(1.0 + (total - df + 0.5) / (df + 0.5))
                denom = tf + self.k1 * (1 - self.b + self.b * length / max(self._avg_length, 1e-6))
                score += idf * (tf * (self.k1 + 1)) / denom
            results.append(score)
        return results

    def search(self, query: str, limit: int) -> List[tuple]:
        """返回 ``[(chunk, score)]``，按分数降序。"""
        scored = list(zip(self._chunks, self.scores(query)))
        scored.sort(key=lambda item: (-item[1], item[0].chunk_id))
        return scored[:limit]

    def coverage(self, query: str) -> List[float]:
        """返回与 ``self._chunks`` 同序的「查询覆盖率」。

        覆盖率 = 命中的去重查询词元数 / 查询去重词元总数，取值 0–1。

        为什么需要它：BM25 分数是**绝对**分数，会被文档长度、词频、IDF 共同放大，
        无法设一个通用阈值卡住误命中。实测「量子力学实验报告」在校园语料上最高
        BM25 分就有 4.16（只因命中「报告」一个词），而「补办学生证需要哪些材料」
        是 13.10——单看绝对分数会把前者当成有效答案。覆盖率是**相对**指标：
        同一个误命中片段只覆盖 1/7 的查询词（0.14），真实命中普遍在 0.4 以上，
        区分度远好于绝对分数，且不随查询长度漂移。
        """
        if not self._chunks:
            return []
        query_terms = set(tokenize(query))
        if not query_terms:
            return [0.0] * len(self._chunks)
        total = len(query_terms)
        coverage: List[float] = []
        for position in range(len(self._chunks)):
            freq = self._term_freqs[position]
            hit = sum(1 for term in query_terms if term in freq)
            coverage.append(hit / total)
        return coverage


def fuse(vector_hits: Sequence[Hit], lexical_hits: Sequence[tuple],
         *, limit: int, alpha: float = 0.5,
         lexical_top_bonus: float = 0.012) -> List[Hit]:
    """融合两路结果（Reciprocal Rank Fusion 的加权变体）。

    使用排名而非原始分数融合：向量余弦与 BM25 分数量纲完全不同，
    直接加权相加没有可比性，按排名融合更稳健。

    ``alpha`` 为向量一路的权重，``1 - alpha`` 给词法一路。

    ``lexical_top_bonus``：BM25 第 1 名的额外加成。原因很实际——
    用户问「需要哪些材料」时，BM25 能精准命中「所需材料」这种短清单段落，
    但该段落向量分低、只能从词法一路得分；若严格按 RRF 相加，
    会被「两路都勉强命中」的长段落压下去。BM25 排名第一是强信号，应显式体现。
    """
    if not 0.0 <= alpha <= 1.0:
        raise ValueError("alpha 必须位于 [0, 1]")

    weights: Dict[str, float] = {}
    payload: Dict[str, Hit] = {}

    # 排名从 1 开始，倒数衰减；加常数避免头部过度主导。
    rrf_k = 60.0
    for rank, hit in enumerate(vector_hits, start=1):
        weights[hit.chunk_id] = weights.get(hit.chunk_id, 0.0) + alpha / (rrf_k + rank)
        payload[hit.chunk_id] = hit
    for rank, (chunk, _score) in enumerate(lexical_hits, start=1):
        contribution = (1 - alpha) / (rrf_k + rank)
        if rank == 1:
            contribution += lexical_top_bonus
        weights[chunk.chunk_id] = weights.get(chunk.chunk_id, 0.0) + contribution
        if chunk.chunk_id not in payload:
            payload[chunk.chunk_id] = Hit(
                chunk_id=chunk.chunk_id,
                source_id=chunk.source_id,
                title=chunk.citation_title,
                content=chunk.content,
                score=0.0,
                metadata=chunk.to_metadata(),
            )

    ordered = sorted(weights, key=lambda cid: (-weights[cid], cid))[:limit]
    fused: List[Hit] = []
    for chunk_id in ordered:
        hit = payload[chunk_id]
        # 对外暴露的 score 用融合排名分：只被词法一路召回的片段没有向量余弦值，
        # 若 score 仍填原始余弦（0.0），下游 min_score 阈值会把正确结果整条丢掉。
        # 原始余弦另存 metadata，排查时还能看到。
        fused.append(Hit(
            chunk_id=hit.chunk_id,
            source_id=hit.source_id,
            title=hit.title,
            content=hit.content,
            score=weights[chunk_id],
            metadata={
                **hit.metadata,
                "fusion_rank_score": round(weights[chunk_id], 6),
                "vector_score": round(hit.score, 6),
            },
        ))
    return fused


def section_match_boost(hit: Hit, query: str) -> float:
    """章节标题与查询的覆盖度加成。

    校园事务问句常常「问什么标题叫什么」——「需要哪些材料」对应「所需材料」，
    「办理步骤」对应「办理步骤」。RRF 只用排名，会漏掉这种强信号：
    某片段向量分不高但章节名精准命中查询主题时，它才是用户要的那一段。

    只取**最末一级标题**（如「5. 材料清单」）参与匹配，不取完整标题路径。
    原因：标题路径里必然包含文档名（「学生证补办…」），而文档名几乎覆盖了
    该文档所有 chunk。若按路径匹配，任何带「学生证」的查询都会让该文档的
    每个 chunk 拿到满分加成，等于退化成「按文档挑一篇」，章节区分度全部丢失。

    返回 0–1 的覆盖度，取查询侧与章节侧覆盖率的较小值：既要求章节名确实
    命中查询词，也要求查询词不会被章节名的无关部分稀释。
    """
    section = str(hit.metadata.get("section") or "")
    if not section:
        return 0.0
    leaf = section.replace(" / ", "/").rsplit("/", 1)[-1]
    leaf = leaf.strip()
    if not leaf:
        return 0.0
    query_terms = set(tokenize(query))
    leaf_terms = set(tokenize(leaf))
    if not query_terms or not leaf_terms:
        return 0.0
    overlap = query_terms & leaf_terms
    if not overlap:
        return 0.0
    return min(len(overlap) / len(leaf_terms), len(overlap) / len(query_terms))


def apply_section_boost(hits: Sequence[Hit], query: str, *, weight: float = 0.02) -> List[Hit]:
    """在融合结果上叠加章节命中加成后重排。

    ``weight`` 与 RRF 分量级（约 1/60 ≈ 0.017）对齐，保证加成足以抬升
    精确标题命中，又不会压过双路命中的强证据。
    """
    if weight <= 0:
        return list(hits)
    rescored = []
    for hit in hits:
        base = float(hit.metadata.get("fusion_rank_score", 0.0))
        boost = weight * section_match_boost(hit, query)
        rescored.append((base + boost, hit))
    rescored.sort(key=lambda item: (-item[0], item[1].chunk_id))
    return [
        Hit(chunk_id=hit.chunk_id, source_id=hit.source_id, title=hit.title,
            content=hit.content, score=hit.score,
            metadata={**hit.metadata, "fusion_rank_score": round(total, 6)})
        for total, hit in rescored
    ]
