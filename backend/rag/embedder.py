"""Embedding 抽象层。

设计意图：团队尚未最终确定 Embedding 方案（本地模型 / 云端 API / 纯词法），
因此业务代码只依赖 ``Embedder`` 接口，切换实现不需要改动 ingest、retriever。

实现：
- ``HashingEmbedder``：纯离线、零依赖、确定性。用字符 n-gram 做特征哈希，
  对中文不需要分词即可工作。检索质量弱于语义模型，但演示与测试稳定可复现。
- ``OpenAICompatEmbedder``：走 OpenAI 兼容的 ``/embeddings`` 端点，
  可用于 SiliconFlow、百炼、DeepSeek 代理等。仅在配置了 API Key 时启用。
- ``build_embedder()``：按环境变量选择，默认离线实现。

注意：更换 Embedder 后向量维度会变化，必须重新执行 ingest 重建索引，
``KnowledgeBase`` 会把 embedder 指纹写入集合名，避免误用旧索引。
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
from abc import ABC, abstractmethod
from typing import Iterable, List, Mapping, Optional, Sequence

# 中文连续字符 + 英文单词，两种粒度都取，兼顾中英混排的校园文档。
_TOKEN_RE = re.compile(r"[\u4e00-\u9fff]+|[A-Za-z]+|[0-9]+")


class Embedder(ABC):
    """向量化接口。所有实现必须满足：同样输入产生同样输出。"""

    #: 写入向量库的标识，用于判断索引是否需要重建。
    name: str = "abstract"

    @property
    @abstractmethod
    def dimensions(self) -> int:
        """向量维度。"""

    @abstractmethod
    def embed_documents(self, texts: Sequence[str]) -> List[List[float]]:
        """批量编码文档片段。"""

    @abstractmethod
    def embed_query(self, text: str) -> List[float]:
        """编码查询。允许与文档使用不同前缀（如 bge 的查询指令）。"""

    def fingerprint(self) -> str:
        return "%s:%d" % (self.name, self.dimensions)


def _ngrams(text: str) -> Iterable[str]:
    """产出检索用特征：中文按 2-gram，英文/数字保留原词并补 3-gram。"""
    for token in _TOKEN_RE.findall(text):
        if "\u4e00" <= token[0] <= "\u9fff":
            # 单字词没有 2-gram，保留单字避免丢失信息。
            if len(token) == 1:
                yield token
            for i in range(len(token) - 1):
                yield token[i : i + 2]
            # 补 3-gram，提升长词（如「学生证补办」）的区分度。
            for i in range(len(token) - 2):
                yield token[i : i + 3]
        else:
            lowered = token.lower()
            yield lowered
            if len(lowered) >= 3:
                for i in range(len(lowered) - 2):
                    yield lowered[i : i + 3]


class HashingEmbedder(Embedder):
    """确定性特征哈希向量，带 sublinear tf 与 L2 归一化。

    不依赖任何模型文件或网络，适合单元测试与离线演示。
    它不是语义模型：同义词、改写说法检索不到，这是明确的取舍。
    """

    def __init__(self, dimensions: int = 512):
        if dimensions < 64:
            raise ValueError("dimensions 至少为 64")
        self._dimensions = dimensions
        self.name = "hashing"

    @property
    def dimensions(self) -> int:
        return self._dimensions

    def _vector(self, text: str) -> List[float]:
        counts: dict = {}
        for gram in _ngrams(text):
            digest = hashlib.blake2b(gram.encode("utf-8"), digest_size=8).digest()
            index = int.from_bytes(digest[:4], "big") % self._dimensions
            # 用第二个字节决定符号，降低哈希碰撞带来的系统性偏置。
            sign = 1.0 if digest[4] & 1 else -1.0
            counts[index] = counts.get(index, 0.0) + sign

        vector = [0.0] * self._dimensions
        for index, value in counts.items():
            # sublinear tf 抑制高频片段的权重。
            vector[index] = math.copysign(1.0 + math.log1p(abs(value)), value)
        norm = math.sqrt(sum(v * v for v in vector))
        if norm > 0:
            vector = [v / norm for v in vector]
        return vector

    def embed_documents(self, texts: Sequence[str]) -> List[List[float]]:
        return [self._vector(t) for t in texts]

    def embed_query(self, text: str) -> List[float]:
        return self._vector(text)


class OpenAICompatEmbedder(Embedder):
    """OpenAI 兼容 ``/embeddings`` 端点。

    仅供已配置云端服务的环境使用；密钥只从环境读取，不写入代码或日志。
    网络失败时抛出 ``EmbeddingError``，由上层决定降级或报 TOOL_UNAVAILABLE。
    """

    def __init__(self, *, api_key: str, model: str, base_url: str,
                 dimensions: int, query_prefix: str = "", batch_size: int = 16):
        if not api_key or not api_key.strip():
            raise ValueError("云端 Embedding 需要 API Key")
        if not model.strip():
            raise ValueError("云端 Embedding 需要模型名")
        self._api_key = api_key
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._dimensions = dimensions
        self._query_prefix = query_prefix
        self._batch_size = max(1, batch_size)
        self.name = "openai-compat:%s" % model

    @property
    def dimensions(self) -> int:
        return self._dimensions

    def _request(self, inputs: Sequence[str]) -> List[List[float]]:
        import httpx

        payload = {"model": self._model, "input": list(inputs)}
        try:
            response = httpx.post(
                self._base_url + "/embeddings",
                json=payload,
                headers={"Authorization": "Bearer %s" % self._api_key},
                timeout=30.0,
            )
            response.raise_for_status()
            body = response.json()
        except Exception as exc:
            # 不回显响应体，避免把请求内容或密钥带进日志与工具结果。
            raise EmbeddingError("云端 Embedding 请求失败") from exc

        try:
            items = sorted(body["data"], key=lambda item: item["index"])
            vectors = [item["embedding"] for item in items]
        except (KeyError, TypeError) as exc:
            raise EmbeddingError("云端 Embedding 返回格式异常") from exc

        if len(vectors) != len(inputs):
            raise EmbeddingError("云端 Embedding 返回条数与输入不一致")
        for vector in vectors:
            if len(vector) != self._dimensions:
                raise EmbeddingError("云端 Embedding 维度与配置不符")
        return vectors

    def embed_documents(self, texts: Sequence[str]) -> List[List[float]]:
        vectors: List[List[float]] = []
        for start in range(0, len(texts), self._batch_size):
            vectors.extend(self._request(texts[start : start + self._batch_size]))
        return vectors

    def embed_query(self, text: str) -> List[float]:
        return self._request([self._query_prefix + text])[0]


class EmbeddingError(RuntimeError):
    """Embedding 不可用。上层应转为 TOOL_UNAVAILABLE 或降级检索。"""


def build_embedder(env: Optional[Mapping[str, str]] = None) -> Embedder:
    """按环境变量构造 Embedder。

    - ``RAG_EMBEDDING_PROVIDER=hashing``（默认）：离线、无需密钥。
    - ``RAG_EMBEDDING_PROVIDER=api``：使用 ``RAG_EMBEDDING_*`` 配置云端服务。
    """
    source = os.environ if env is None else env
    provider = (source.get("RAG_EMBEDDING_PROVIDER") or "hashing").strip().lower()

    if provider == "hashing":
        raw = (source.get("RAG_EMBEDDING_DIMENSIONS") or "512").strip()
        try:
            dimensions = int(raw)
        except ValueError:
            raise ValueError("RAG_EMBEDDING_DIMENSIONS 必须是整数")
        return HashingEmbedder(dimensions=dimensions)

    if provider == "api":
        api_key = (source.get("RAG_EMBEDDING_API_KEY") or source.get("LLM_API_KEY") or "").strip()
        model = (source.get("RAG_EMBEDDING_MODEL") or "").strip()
        base_url = (source.get("RAG_EMBEDDING_BASE_URL") or "").strip()
        if not base_url:
            raise ValueError("使用 api Embedding 时必须设置 RAG_EMBEDDING_BASE_URL")
        try:
            dimensions = int((source.get("RAG_EMBEDDING_DIMENSIONS") or "").strip())
        except ValueError:
            raise ValueError("使用 api Embedding 时必须显式设置 RAG_EMBEDDING_DIMENSIONS")
        return OpenAICompatEmbedder(
            api_key=api_key,
            model=model,
            base_url=base_url,
            dimensions=dimensions,
            query_prefix=source.get("RAG_EMBEDDING_QUERY_PREFIX") or "",
        )

    raise ValueError("RAG_EMBEDDING_PROVIDER 仅支持 hashing 或 api")
