"""校园知识文档的解析与切分。

CampusFlow 要求知识问答「附有依据」，所以每个 chunk 都必须能追溯到
原始文件与标题层级，``source_id`` 在检索结果里回传给用户引用。

支持的输入：
- ``.md``：按 Markdown 标题切分，保留标题路径。
- ``.txt``：按空行分段。
- ``.pdf``：需要 ``pypdf``，按页提取文本后按段落切分，记录页码。

每篇文档需要在文件头部声明来源属性（参见 ``data/knowledge/README.md``）：

```markdown
---
title: 学生证补办流程
source: 模拟文档
source_type: simulated
url:
updated_at: 2026-10-01
---
```

``source_type`` 只允许 ``simulated``（模拟整理）与 ``official``（真实官方文件）。
缺少该字段时一律按 ``simulated`` 处理，避免把来路不明的内容标成官方规定。
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

SUPPORTED_SUFFIXES = {".md", ".markdown", ".txt", ".pdf"}

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
# 列表项：- / * / + / • 开头，或 1. / 1、 / (1) 编号开头。
_LIST_ITEM_RE = re.compile(r"^\s*(?:[-*+•]|\d+[.、)]|\(\d+\))\s*\S")
# 中文句子边界：句号、问号、感叹号、分号，保留分隔符。
_SENTENCE_RE = re.compile(r"[^。！？；\n]+[。！？；]?")


class DocumentError(ValueError):
    """文档无法解析。"""


@dataclass(frozen=True)
class DocumentMeta:
    """文档级元信息，缺省值刻意保守。"""

    title: str
    source_type: str = "simulated"
    source: str = "未标注来源"
    url: str = ""
    updated_at: str = ""

    def __post_init__(self):
        if self.source_type not in {"simulated", "official"}:
            raise DocumentError("source_type 仅支持 simulated 或 official")
        if not self.title.strip():
            raise DocumentError("文档必须有标题")


@dataclass(frozen=True)
class Chunk:
    """一个可检索片段，已带上完整的引用信息。"""

    chunk_id: str
    source_id: str
    title: str
    section: str
    content: str
    order: int
    meta: DocumentMeta
    page: Optional[int] = None

    @property
    def citation_title(self) -> str:
        """给用户看的标题，带上级标题路径。"""
        return self.title if not self.section else "%s · %s" % (self.title, self.section)

    def to_metadata(self) -> Dict[str, object]:
        """写入向量库的扁平元数据；Chroma 只接受标量值。"""
        return {
            "source_id": self.source_id,
            "title": self.citation_title,
            "doc_title": self.title,
            "section": self.section,
            "order": self.order,
            "source_type": self.meta.source_type,
            "source": self.meta.source,
            "url": self.meta.url,
            "updated_at": self.meta.updated_at,
            "page": self.page if self.page is not None else 0,
        }


def _unsafe_id(value: str) -> bool:
    """source_id 会展示在引用里，只拦截路径分隔符与控制字符。"""
    if not value or not value.strip():
        return True
    if len(value) > 60:
        return True
    if any(ch in value for ch in "/\\:*?\"<>|"):
        return True
    return any(ord(ch) < 32 for ch in value)


def _parse_front_matter(text: str) -> tuple:
    """解析文件头部的简易 YAML（只支持 key: value），返回 (meta_dict, 正文)。"""
    if not text.lstrip().startswith("---"):
        return {}, text
    lines = text.lstrip().splitlines()
    if lines[0].strip() != "---":
        return {}, text

    meta: Dict[str, str] = {}
    for index in range(1, len(lines)):
        line = lines[index]
        if line.strip() == "---":
            return meta, "\n".join(lines[index + 1 :])
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            raise DocumentError("文件头格式错误，应为 key: value：%s" % line[:40])
        key, value = line.split(":", 1)
        meta[key.strip()] = value.strip().strip('"').strip("'")
    raise DocumentError("文件头缺少结束的 ---")


def _split_long(text: str, max_chars: int, overlap: int) -> List[str]:
    """把超长段落按行/句子边界切成若干段，段间允许少量重叠以保持上下文。

    列表块优先按行切分：直接按句号切会把多个列表项拼成一行，
    失去「一条材料一行」的可读性，模型也更容易误读条目边界。
    """
    stripped = text.strip()
    if len(stripped) <= max_chars:
        return [stripped] if stripped else []

    lines = [line.rstrip() for line in stripped.splitlines()]
    is_list = sum(1 for line in lines if _LIST_ITEM_RE.match(line)) >= max(2, len(lines) // 2)

    if is_list:
        pieces: List[str] = []
        current = ""
        for line in lines:
            if not line.strip():
                continue
            if not current:
                current = line
            elif len(current) + 1 + len(line) <= max_chars:
                current += "\n" + line
            else:
                pieces.append(current)
                current = line
        if current:
            pieces.append(current)
        return pieces

    return _split_by_sentence(stripped, max_chars, overlap)


def _split_by_sentence(text: str, max_chars: int, overlap: int) -> List[str]:
    sentences = [s.strip() for s in _SENTENCE_RE.findall(text) if s.strip()]
    if not sentences:
        return [text.strip()] if text.strip() else []

    pieces: List[str] = []
    current = ""
    for sentence in sentences:
        # 单句本身就超长时硬切，避免产出超大 chunk。
        while len(sentence) > max_chars:
            if current:
                pieces.append(current)
                current = ""
            pieces.append(sentence[:max_chars])
            sentence = sentence[max_chars:]
        if len(current) + len(sentence) <= max_chars:
            current += sentence
        else:
            if current:
                pieces.append(current)
            current = sentence
    if current:
        pieces.append(current)

    if overlap > 0 and len(pieces) > 1:
        merged: List[str] = [pieces[0]]
        for piece in pieces[1:]:
            tail = merged[-1][-overlap:]
            merged.append(tail + piece)
        return merged
    return pieces


def _chunk_markdown(text: str, meta: DocumentMeta, source_id: str,
                    max_chars: int, overlap: int) -> List[Chunk]:
    """按标题层级切分，每个段落连同所属标题路径成为独立 chunk。

    空行是默认的段落边界，但列表块例外：连续的 ``-`` / ``1.`` 列表项
    （中间允许空行）必须留在同一个 chunk 里。否则「所需材料」这类清单会被
    切成若干个只有几个字的碎片，既检索不到也读不懂。
    """
    chunks: List[Chunk] = []
    headings: Dict[int, str] = {}
    buffer: List[str] = []
    order = 0

    def current_section() -> str:
        return " / ".join(headings[level] for level in sorted(headings))

    def flush() -> None:
        nonlocal buffer, order
        body = _collapse_blank("\n".join(buffer)).strip()
        buffer = []
        if not body:
            return
        for piece in _split_long(body, max_chars, overlap):
            chunks.append(Chunk(
                chunk_id="%s-%03d" % (source_id, order),
                source_id=source_id,
                title=meta.title,
                section=current_section(),
                content=piece,
                order=order,
                meta=meta,
            ))
            order += 1

    for line in text.splitlines():
        match = _HEADING_RE.match(line)
        if match:
            flush()
            level = len(match.group(1))
            # 出现更高级标题时，清掉所有同级及更深的旧标题。
            for existing in [lv for lv in headings if lv >= level]:
                del headings[existing]
            headings[level] = match.group(2).strip()
        elif not line.strip():
            # 段落之间断开，避免把不相关内容拼进同一个 chunk；
            # 但列表块内部不断开，交给 _collapse_blank 处理连续列表的拼接。
            if buffer and any(_LIST_ITEM_RE.match(item) for item in buffer if item.strip()):
                buffer.append("")
                continue
            flush()
        else:
            buffer.append(line)
    flush()
    return chunks


def _collapse_blank(text: str) -> str:
    """把列表项之间的空行去掉，让一份清单成为一个完整段落。

    只在「两侧都是列表项」时删除空行；普通段落之间的空行已经由 flush 处理。
    """
    lines = text.splitlines()
    out: List[str] = []
    for index, line in enumerate(lines):
        if line.strip():
            out.append(line)
            continue
        previous = next((l for l in reversed(out) if l.strip()), "")
        following = next((l for l in lines[index + 1:] if l.strip()), "")
        if _LIST_ITEM_RE.match(previous) and _LIST_ITEM_RE.match(following):
            continue
        out.append(line)
    return "\n".join(out)


def _chunk_plain(text: str, meta: DocumentMeta, source_id: str,
                 max_chars: int, overlap: int, page: Optional[int] = None) -> List[Chunk]:
    chunks: List[Chunk] = []
    order = 0
    for block in re.split(r"\n\s*\n", text):
        block = block.strip()
        if not block:
            continue
        for piece in _split_long(block, max_chars, overlap):
            chunks.append(Chunk(
                chunk_id="%s-%03d" % (source_id, order),
                source_id=source_id,
                title=meta.title,
                section="",
                content=piece,
                order=order,
                meta=meta,
                page=page,
            ))
            order += 1
    return chunks


def _read_pdf(path: Path) -> List[tuple]:
    """返回 [(页码, 文本)]。缺少 pypdf 时给出明确安装提示。"""
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise DocumentError(
            "解析 PDF 需要 pypdf，请执行：pip install pypdf"
        ) from exc

    try:
        reader = PdfReader(str(path))
        return [(index + 1, page.extract_text() or "") for index, page in enumerate(reader.pages)]
    except Exception as exc:
        raise DocumentError("PDF 解析失败：%s" % path.name) from exc


def load_document(path: Path, *, source_id: Optional[str] = None,
                  max_chars: int = 400, overlap: int = 60) -> List[Chunk]:
    """解析单个文件为 chunk 列表。

    ``source_id`` 的取值优先级：显式参数 > 文件头的 ``source_id`` > 文件名（不含扩展名）。
    中文可用于 source_id——它会作为引用编号展示给用户，「学生证补办」比
    「doc-eec2d813e1」更易核对。仅当文件名含路径分隔符等不安全字符时才做哈希兜底。
    """
    path = Path(path)
    if path.suffix.lower() not in SUPPORTED_SUFFIXES:
        raise DocumentError("不支持的文档类型：%s" % path.suffix)
    if not path.is_file():
        raise DocumentError("文件不存在：%s" % path)

    # 先读文件头拿到可能声明的 source_id，再决定最终编号。
    declared: Dict[str, str] = {}
    if path.suffix.lower() != ".pdf":
        try:
            declared, _ = _parse_front_matter(path.read_text(encoding="utf-8"))
        except (DocumentError, UnicodeDecodeError):
            declared = {}

    raw_id = source_id or declared.get("source_id") or path.stem
    if _unsafe_id(raw_id):
        raw_id = "doc-" + hashlib.sha1(raw_id.encode("utf-8")).hexdigest()[:10]


    if path.suffix.lower() == ".pdf":
        pages = _read_pdf(path)
        head = pages[0][1] if pages else ""
        meta_fields, _ = _parse_front_matter(head)
        meta = _meta_from(meta_fields, fallback_title=path.stem)
        raw: List[Chunk] = []
        for page_no, text in pages:
            if not text.strip():
                continue
            # PDF 首页可能含文件头，正文同样用 front matter 解析器剥掉。
            _, body = _parse_front_matter(text)
            raw.extend(_chunk_plain(body, meta, raw_id, max_chars, overlap, page=page_no))
        # 页码内各自计数会撞号，这里统一重编号保证 chunk_id 全局唯一。
        return [
            Chunk(chunk_id="%s-%03d" % (raw_id, index), source_id=c.source_id,
                  title=c.title, section=c.section, content=c.content,
                  order=index, meta=c.meta, page=c.page)
            for index, c in enumerate(raw)
        ]

    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise DocumentError("文档必须为 UTF-8 编码：%s" % path.name) from exc

    meta_fields, body = _parse_front_matter(text)
    meta = _meta_from(meta_fields, fallback_title=path.stem)

    if path.suffix.lower() in {".md", ".markdown"}:
        return _chunk_markdown(body, meta, raw_id, max_chars, overlap)
    return _chunk_plain(body, meta, raw_id, max_chars, overlap)


def _meta_from(fields: Dict[str, str], *, fallback_title: str) -> DocumentMeta:
    return DocumentMeta(
        title=fields.get("title") or fallback_title,
        source_type=fields.get("source_type") or "simulated",
        source=fields.get("source") or "未标注来源",
        url=fields.get("url") or "",
        updated_at=fields.get("updated_at") or "",
    )


def load_directory(directory: Path, *, max_chars: int = 400,
                   overlap: int = 60) -> List[Chunk]:
    """解析目录下全部受支持文件，按文件名排序保证结果可复现。

    目录里的 ``README.md`` 是给人看的写作规范，不是校园政策内容，
    如果被检索到会污染结果（问"学生证丢了怎么办"却返回建索引命令），
    所以一律跳过。需要给某份文档排除时，把文件名改为 ``.README.md`` 之外的
    前缀（如 ``_draft_xxx.md`` 仍会入库），或直接移出目录。
    """
    directory = Path(directory)
    if not directory.is_dir():
        return []
    chunks: List[Chunk] = []
    for path in sorted(directory.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() not in SUPPORTED_SUFFIXES:
            continue
        if path.name.lower() == "readme.md":
            continue
        chunks.extend(load_document(path, max_chars=max_chars, overlap=overlap))
    return chunks
