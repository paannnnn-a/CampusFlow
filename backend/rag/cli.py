"""知识库构建 CLI。

用法：

```bash
python -m backend.rag.cli rebuild          # 重建索引
python -m backend.rag.cli stats            # 查看索引统计
python -m backend.rag.cli search 学生证    # 试检索
```

环境变量见 ``.env.example`` 的 RAG_* 段。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .knowledge_base import build_knowledge_base


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="backend.rag.cli", description="校园知识库管理")
    parser.add_argument("command", choices=["rebuild", "stats", "search"])
    parser.add_argument("query", nargs="?", default="", help="search 命令的查询词")
    parser.add_argument("--sources", default="data/knowledge", help="知识文档目录")
    parser.add_argument("--limit", type=int, default=5, help="检索返回条数")
    args = parser.parse_args(argv)

    kb = build_knowledge_base()
    sources = Path(args.sources)

    if args.command == "rebuild":
        count = kb.build_from_directory(sources)
        print(json.dumps({"indexed": count, **kb.stats()}, ensure_ascii=False, indent=2))
        if count == 0:
            print("警告：未找到任何文档。请把知识文件放入 %s" % args.sources, file=sys.stderr)
            return 1
        return 0

    # CLI 是短进程，每次都要重新灌一次文档，索引才有内容可查。
    if args.command in {"stats", "search"}:
        count = kb.build_from_directory(sources)
        if count == 0 and args.command == "search":
            print("警告：未找到任何文档。请把知识文件放入 %s" % args.sources, file=sys.stderr)

    if args.command == "stats":
        print(json.dumps(kb.stats(), ensure_ascii=False, indent=2))
        return 0

    if not args.query.strip():
        parser.error("search 命令需要查询词")
    outcome = kb.search(args.query, limit=args.limit)
    print(json.dumps({
        "query": args.query,
        "total_indexed": outcome.total_indexed,
        "min_score": outcome.min_score,
        "hits": [
            {
                "chunk_id": hit.chunk_id,
                "source_id": hit.source_id,
                "title": hit.title,
                "score": round(hit.score, 4),
                "content": hit.content[:120],
            }
            for hit in outcome.hits
        ],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
