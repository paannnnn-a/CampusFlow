"""知识检索工具：``search_campus_knowledge``。

对应 ``tool_contract.md`` 的返回结构：

```json
{"sources": [{"source_id": "...", "title": "...", "content": "..."}]}
```

扩展字段（允许，不影响 A 的解析）：
- ``source_type``：``simulated`` / ``official``，前端据此显示「模拟资料」角标。
- ``url`` / ``page``：真实文件地址或页码，便于核对。
- ``total_indexed``：索引内片段总数，用于区分「知识库为空」与「确实没查到」。

无依据时返回 ``sources: []``，message 明确说明未查到，绝不虚构内容。
"""
from __future__ import annotations

from typing import Any, Dict

from ..agent.contracts import Context, KnowledgeArgs
from ..rag.knowledge_base import KnowledgeBase
from ._common import read_tool

#: 返回给模型的证据条数上限。太少不足以支撑答复，太多会挤占上下文。
DEFAULT_TOP_K = 4
#: 单条证据的正文字符上限，避免超长片段压垮模型上下文。
MAX_CONTENT_CHARS = 600


class KnowledgeTools:
    """知识检索工具的绑定器。"""

    def __init__(self, knowledge_base: KnowledgeBase, *,
                 top_k: int = DEFAULT_TOP_K,
                 max_content_chars: int = MAX_CONTENT_CHARS):
        if top_k < 1:
            raise ValueError("top_k 必须为正整数")
        self.kb = knowledge_base
        self.top_k = top_k
        self.max_content_chars = max_content_chars
        self.search_campus_knowledge = read_tool(self._search)

    # ------------------------------------------------------------ 业务

    def _search(self, args: KnowledgeArgs, context: Context):
        outcome = self.kb.search(args.query, limit=self.top_k)
        if outcome.total_indexed == 0:
            # 索引为空与「查不到」是两件事，分开提示，避免误导向换关键词。
            return (
                {"sources": [], "total_indexed": 0},
                "知识库尚未建立索引，请先运行知识入库；当前无法提供政策依据。",
            )
        if not outcome.found:
            return (
                {"sources": [], "total_indexed": outcome.total_indexed},
                "未在校园资料中检索到相关依据。请换用更具体的事务名称，"
                "或直接咨询对应部门；不要凭猜测回答。",
            )

        sources = []
        for hit in outcome.hits:
            content = hit.content.strip()
            if len(content) > self.max_content_chars:
                content = content[: self.max_content_chars].rstrip() + "……"
            item: Dict[str, Any] = {
                "source_id": hit.source_id,
                "title": hit.title,
                "content": content,
                "source_type": hit.source_type,
                "score": round(hit.score, 4),
            }
            if hit.url:
                item["url"] = hit.url
            if hit.page:
                item["page"] = hit.page
            sources.append(item)

        simulated = any(s["source_type"] == "simulated" for s in sources)
        message = "检索到 %d 条校园资料" % len(sources)
        if simulated:
            message += "（含模拟整理资料，答复时须告知用户其为模拟内容）"
        return {"sources": sources, "total_indexed": outcome.total_indexed}, message

    # ------------------------------------------------------------ 绑定

    def bind(self, registry) -> None:
        registry.bind("search_campus_knowledge", self.search_campus_knowledge, simulated=True)
