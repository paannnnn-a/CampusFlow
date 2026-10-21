"""RAG 层测试：解析、切分、来源标注、检索、阈值、降级。

不依赖 chromadb：使用 ``MemoryIndex`` 与 ``HashingEmbedder``，
保证任何环境 clone 下来都能跑通。
"""
from __future__ import annotations

import pytest

from backend.rag import (
    Chunk,
    DocumentError,
    HashingEmbedder,
    KnowledgeBase,
    MemoryIndex,
    load_directory,
    load_document,
)
from backend.rag.ingest import _parse_front_matter


def _write(path, text):
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def sample_md(tmp_path):
    return _write(tmp_path / "学生证补办.md", """---
title: 学生证补办流程
source_type: simulated
source: 模拟整理，依据常见高校办事流程
updated_at: 2026-10-01
---

## 适用情形

学生证遗失、损坏需要补办时使用本流程。请先携带身份证到学院办公室登记。

## 所需材料

- 身份证原件与复印件
- 一寸证件照两张
- 遗失情况说明

## 办理步骤

1. 到学院办公室提交材料并填写申请表。
2. 等待教务处制证，一般需要五个工作日。
""")


@pytest.fixture
def knowledge_base():
    embedder = HashingEmbedder(dimensions=512)
    return KnowledgeBase(embedder=embedder, index=MemoryIndex(), min_score=0.1)


# ------------------------------------------------------------------ 解析


def test_front_matter_is_parsed_and_stripped():
    meta, body = _parse_front_matter("---\ntitle: 测试\nsource: 某处\n---\n正文内容")
    assert meta["title"] == "测试"
    assert "正文内容" in body
    assert "---" not in body


def test_front_matter_requires_closing_marker():
    with pytest.raises(DocumentError):
        _parse_front_matter("---\ntitle: 测试\n正文")


def test_markdown_splits_by_heading_with_section_path(sample_md):
    chunks = load_document(sample_md)
    assert chunks, "应解析出片段"
    sections = {chunk.section for chunk in chunks}
    assert any("所需材料" in s for s in sections)
    assert any("办理步骤" in s for s in sections)
    # 标题不应混入正文
    assert all("##" not in chunk.content for chunk in chunks)


def test_metadata_carries_source_attribution(sample_md):
    chunks = load_document(sample_md)
    meta = chunks[0].meta
    assert meta.title == "学生证补办流程"
    assert meta.source_type == "simulated"
    assert "模拟" in meta.source
    assert chunks[0].to_metadata()["source_type"] == "simulated"


def test_missing_source_type_defaults_to_simulated(tmp_path):
    """没有声明 source_type 的文档必须按模拟处理，不能默认成官方。"""
    path = _write(tmp_path / "unknown.md", "# 标题\n\n正文")
    chunks = load_document(path)
    assert chunks[0].meta.source_type == "simulated"


def test_invalid_source_type_is_rejected(tmp_path):
    path = _write(tmp_path / "bad.md", "---\ntitle: 测试\nsource_type: 官方\n---\n正文")
    with pytest.raises(DocumentError):
        load_document(path)


def test_chunk_ids_are_unique_and_ordered(sample_md):
    chunks = load_document(sample_md)
    ids = [chunk.chunk_id for chunk in chunks]
    assert len(ids) == len(set(ids))
    assert [chunk.order for chunk in chunks] == list(range(len(chunks)))


def test_long_paragraph_is_split_within_limit(tmp_path):
    long_text = "这是一个用于测试切分逻辑的句子。" * 60
    path = _write(tmp_path / "long.md", "# 长文\n\n" + long_text)
    chunks = load_document(path, max_chars=200, overlap=20)
    assert len(chunks) > 1
    # 允许 overlap 带来的少量超出，但不应显著超过上限。
    assert all(len(chunk.content) <= 200 + 20 for chunk in chunks)


def test_unsupported_suffix_is_rejected(tmp_path):
    path = _write(tmp_path / "doc.docx", "x")
    with pytest.raises(DocumentError):
        load_document(path)


def test_load_directory_reads_all_supported_files(tmp_path, sample_md):
    _write(tmp_path / "随手记.txt", "图书馆开放时间：早上八点到晚上十点。")
    chunks = load_directory(tmp_path)
    titles = {chunk.title for chunk in chunks}
    assert "学生证补办流程" in titles
    assert "随手记" in titles


def test_load_directory_returns_empty_for_missing_dir(tmp_path):
    assert load_directory(tmp_path / "nope") == []


# ------------------------------------------------------------------ 检索


def test_build_then_search_returns_expected_source(sample_md, knowledge_base):
    count = knowledge_base.build(load_document(sample_md))
    assert count > 0
    outcome = knowledge_base.search("学生证丢了怎么补办，需要什么材料")
    assert outcome.found
    # source_id 用可读的文件名，而不是哈希，用户才能核对依据。
    assert outcome.hits[0].source_id == "学生证补办"
    assert outcome.hits[0].title.startswith("学生证补办流程")


def test_material_question_ranks_material_section(sample_md, knowledge_base):
    """问「需要什么材料」时，材料段应进入结果集，证明切分粒度可用。"""
    knowledge_base.build(load_document(sample_md))
    outcome = knowledge_base.search("补办学生证需要准备哪些材料", limit=4)
    assert outcome.found
    joined = " ".join(hit.content for hit in outcome.hits)
    assert "证件照" in joined or "复印件" in joined


def test_search_on_empty_index_returns_no_hits(knowledge_base):
    outcome = knowledge_base.search("任何问题")
    assert not outcome.found
    assert outcome.total_indexed == 0


def test_search_rejects_blank_query(knowledge_base, sample_md):
    knowledge_base.build(load_document(sample_md))
    assert not knowledge_base.search("   ").found


def test_irrelevant_query_is_filtered_by_threshold(sample_md, knowledge_base):
    knowledge_base.build(load_document(sample_md))
    # 「量子力学实验报告」与文档无关，阈值应把它挡掉。
    outcome = knowledge_base.search("量子力学实验报告格式", min_score=0.3)
    assert not outcome.found


def test_rebuild_replaces_old_content(sample_md, knowledge_base, tmp_path):
    knowledge_base.build(load_document(sample_md))
    other = _write(tmp_path / "other.md", "# 图书馆规则\n\n图书馆不得携带食物入内。")
    knowledge_base.build(load_document(other), reset=True)
    assert knowledge_base.index.count() == len(load_document(other))
    assert not knowledge_base.search("学生证补办", min_score=0.3).found


def test_hashing_embedder_is_deterministic():
    embedder = HashingEmbedder(dimensions=256)
    first = embedder.embed_query("学生证补办")
    second = embedder.embed_query("学生证补办")
    assert first == second
    assert len(first) == 256


def test_vector_dimension_mismatch_is_reported():
    short = MemoryIndex()
    chunk = Chunk(chunk_id="c1", source_id="s", title="t", section="",
                  content="内容", order=0, meta=_meta())
    short.add([chunk], [[1.0, 0.0]])
    # 维度不一致时必须明确报错，而不是返回错误的相似度。
    with pytest.raises(ValueError):
        short.search([1.0, 0.0, 0.0], limit=1)


def test_stats_reports_embedder_fingerprint(sample_md, knowledge_base):
    knowledge_base.build(load_document(sample_md))
    stats = knowledge_base.stats()
    assert stats["indexed"] > 0
    assert stats["embedder"].startswith("hashing")


def _meta():
    from backend.rag.ingest import DocumentMeta

    return DocumentMeta(title="测试")


# ---------------------------------------------------------------- 回归用例
# 以下用例针对真实语料（data/knowledge）暴露过的三个缺陷，
# 用合成数据也应当成立，避免换语料后再次退化。


def test_lexical_only_hit_keeps_nonzero_score(tmp_path):
    """只被词法一路召回的片段，score 不能是 0。

    缺陷背景：fuse 曾把 ``score`` 填成原始向量余弦值，纯 BM25 命中没有余弦值
    就填 0。下游 ``min_score`` 阈值按 score 过滤，会把正确结果整条丢掉，
    表现是「关键词明明在文档里，但检索返回空」。
    """
    path = _write(tmp_path / "材料清单.md", """---
title: 证件办理
source_type: simulated
source: 测试
---

## 描述

这里讲的是完全无关的宿舍床位安排内容，用词刻意避开证件与清单。

## 所需材料

身份证原件、一寸照片、申请表。
""")
    kb = KnowledgeBase(embedder=HashingEmbedder(dimensions=256), index=MemoryIndex())
    kb.build(load_document(path))
    outcome = kb.search("所需材料有哪些", limit=3)
    assert outcome.found
    # 第一名的分数必须为正，否则说明又退回了「原始余弦填充 score」的写法。
    assert outcome.hits[0].score > 0
    # 原始向量余弦仍应保留在 metadata 中，便于排查。
    assert "vector_score" in outcome.hits[0].metadata


def test_section_boost_uses_leaf_heading_not_full_path(tmp_path):
    """章节加成只看最末一级标题。

    缺陷背景：曾用完整标题路径（含文档名）计算加成，文档名覆盖该文档所有
    chunk，导致任何包含文档名的查询都给全文满分加成，章节区分度全部丢失。
    """
    path = _write(tmp_path / "学生证补办.md", """---
title: 学生证补办
source_type: simulated
source: 测试
---

## 适用情形

学生证遗失可以补办。

## 所需材料

身份证原件与复印件。
""")
    kb = KnowledgeBase(embedder=HashingEmbedder(dimensions=256), index=MemoryIndex())
    kb.build(load_document(path))
    from backend.rag.hybrid import section_match_boost

    outcome = kb.search("所需材料", limit=4)
    assert outcome.found
    boosts = {h.metadata.get("section", ""): section_match_boost(h, "所需材料")
              for h in outcome.hits}
    # 「所需材料」一节必须有加成；「适用情形」一节不应因为文档名含「学生证」而得加成。
    for section, boost in boosts.items():
        if section.endswith("所需材料"):
            assert boost > 0
        else:
            assert boost == 0.0


def test_load_directory_skips_readme(tmp_path):
    """目录里的 README 是写作规范，不是校园政策，不能被检索到。

    缺陷背景：README 讲的是重建索引命令，问「学生证丢了怎么办」时
    曾返回建索引的 bash 片段，属于明显错答。
    """
    _write(tmp_path / "README.md", """---
title: 目录说明
source_type: simulated
source: 测试
---

# 说明

运行 python -m backend.rag.cli rebuild 重建索引。
""")
    _write(tmp_path / "学生证补办.md", """---
title: 学生证补办
source_type: simulated
source: 测试
---

## 材料

身份证原件。
""")
    chunks = load_directory(tmp_path)
    assert chunks
    assert all(c.source_id != "README" for c in chunks)


# ---------------------------------------------------------------- 回归用例
# 以下用例针对真实语料（data/knowledge）暴露过的三个缺陷，
# 用合成数据也应当成立，避免换语料后再次退化。


def test_lexical_only_hit_keeps_nonzero_score(tmp_path):
    """只被词法一路召回的片段，score 不能是 0。

    缺陷背景：fuse 曾把 ``score`` 填成原始向量余弦值，纯 BM25 命中没有余弦值
    就填 0。下游 ``min_score`` 阈值按 score 过滤，会把正确结果整条丢掉，
    表现是「关键词明明在文档里，但检索返回空」。
    """
    path = _write(tmp_path / "材料清单.md", """---
title: 证件办理
source_type: simulated
source: 测试
---

## 描述

这里讲的是完全无关的宿舍床位安排内容，用词刻意避开证件与清单。

## 所需材料

身份证原件、一寸照片、申请表。
""")
    kb = KnowledgeBase(embedder=HashingEmbedder(dimensions=256), index=MemoryIndex())
    kb.build(load_document(path))
    outcome = kb.search("所需材料有哪些", limit=3)
    assert outcome.found
    # 第一名的分数必须为正，否则说明又退回了「原始余弦填充 score」的写法。
    assert outcome.hits[0].score > 0
    # 原始向量余弦仍应保留在 metadata 中，便于排查。
    assert "vector_score" in outcome.hits[0].metadata


def test_section_boost_uses_leaf_heading_not_full_path(tmp_path):
    """章节加成只看最末一级标题。

    缺陷背景：曾用完整标题路径（含文档名）计算加成，文档名覆盖该文档所有
    chunk，导致任何包含文档名的查询都给全文满分加成，章节区分度全部丢失。
    """
    path = _write(tmp_path / "学生证补办.md", """---
title: 学生证补办
source_type: simulated
source: 测试
---

## 适用情形

学生证遗失可以补办。

## 所需材料

身份证原件与复印件。
""")
    kb = KnowledgeBase(embedder=HashingEmbedder(dimensions=256), index=MemoryIndex())
    kb.build(load_document(path))
    from backend.rag.hybrid import section_match_boost

    outcome = kb.search("所需材料", limit=4)
    assert outcome.found
    boosts = {h.metadata.get("section", ""): section_match_boost(h, "所需材料")
              for h in outcome.hits}
    # 「所需材料」一节必须有加成；「适用情形」一节不应因为文档名含「学生证」而得加成。
    for section, boost in boosts.items():
        if section.endswith("所需材料"):
            assert boost > 0
        else:
            assert boost == 0.0


def test_load_directory_skips_readme(tmp_path):
    """目录里的 README 是写作规范，不是校园政策，不能被检索到。

    缺陷背景：README 讲的是重建索引命令，问「学生证丢了怎么办」时
    曾返回建索引的 bash 片段，属于明显错答。
    """
    _write(tmp_path / "README.md", """---
title: 目录说明
source_type: simulated
source: 测试
---

# 说明

运行 python -m backend.rag.cli rebuild 重建索引。
""")
    _write(tmp_path / "学生证补办.md", """---
title: 学生证补办
source_type: simulated
source: 测试
---

## 材料

身份证原件。
""")
    chunks = load_directory(tmp_path)
    assert chunks
    assert all(c.source_id != "README" for c in chunks)


def test_off_domain_query_returns_nothing(tmp_path):
    """语料里没有的内容必须返回空，不能「撞上一个词就当答案」。

    缺陷背景：BM25 绝对分会命中「报告」这类高频词，问「量子力学实验报告」
    曾返回学生证补办的材料清单。仅靠绝对分数阈值卡不住（误命中也有 4 分，
    而短查询的正确命中可能只有 3 分），所以加了查询覆盖率下限。
    """
    path = _write(tmp_path / "学生证补办.md", """---
title: 学生证补办
source_type: simulated
source: 测试
---

## 材料清单

身份证原件、补办申请表、本人书面遗失报告。
""")
    kb = KnowledgeBase(embedder=HashingEmbedder(dimensions=256), index=MemoryIndex())
    kb.build(load_document(path))
    # 只命中「报告」，覆盖率 1/7，应被挡掉。
    assert not kb.search("量子力学实验报告").found
    assert not kb.search("今天天气怎么样").found
    # 同一语料下的真实问题仍要能命中。
    assert kb.search("补办需要哪些材料").found


def test_off_domain_query_returns_nothing(tmp_path):
    """语料里没有的内容必须返回空，不能「撞上一个词就当答案」。

    缺陷背景：BM25 绝对分会命中「报告」这类高频词，问「量子力学实验报告」
    曾返回学生证补办的材料清单。仅靠绝对分数阈值卡不住（误命中也有 4 分，
    而短查询的正确命中可能只有 3 分），所以加了查询覆盖率下限。
    """
    path = _write(tmp_path / "学生证补办.md", """---
title: 学生证补办
source_type: simulated
source: 测试
---

## 材料清单

身份证原件、补办申请表、本人书面遗失报告。
""")
    kb = KnowledgeBase(embedder=HashingEmbedder(dimensions=256), index=MemoryIndex())
    kb.build(load_document(path))
    # 只命中「报告」，覆盖率 1/7，应被挡掉。
    assert not kb.search("量子力学实验报告").found
    assert not kb.search("今天天气怎么样").found
    # 同一语料下的真实问题仍要能命中。
    assert kb.search("补办需要哪些材料").found
