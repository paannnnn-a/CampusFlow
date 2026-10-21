"""B 侧工具注册入口。

集成时（A 或 D 的启动代码中）：

```python
from backend.agent.tool_registry import ToolRegistry
from backend.tools import bind_b_tools

registry = ToolRegistry()
bundle = bind_b_tools(registry)   # 覆盖 knowledge/affair/dormitory 共 5 个工具
```

未绑定的 C 侧工具保持 UNKNOWN_TOOL，A 侧无需改动即可继续用 Mock 开发。

**关于 Mock 替换**：``MockCampus.registry()`` 会把 13 个工具全部绑到内存实现上。
集成阶段应在 Mock 注册之后调用本函数，覆盖 B 负责的 5 个工具，
使 A 的 C 侧工具继续使用 Mock，而 B 侧走真实业务与数据库。

**关于持久化**：默认用 ``SqlRepository``（SQLite）落盘，满足验收要求
「草稿与报修单要真的写进数据库、能按编号查回」。传
``storage="memory"`` 或在环境里设 ``CPAFFAIRS_STORAGE=memory``
可退回内存实现，适合单测与临时演示（进程退出即丢数据）。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional

from ..database.b_repository import CampusRepository, InMemoryRepository
from ..database.b_sqlite import SqlRepository
from ..rag.knowledge_base import KnowledgeBase, build_knowledge_base
from ..services.affairs import AffairService
from ..services.dormitory import DormitoryService
from .affair_tools import AffairTools
from .dormitory_tools import DormitoryTools
from .knowledge_tools import KnowledgeTools

#: B 负责的工具名。集成方可据此核对覆盖范围。
B_TOOL_NAMES = (
    "search_campus_knowledge",
    "get_affair_requirements",
    "create_affair_draft",
    "create_repair_ticket",
    "get_repair_status",
)

#: 默认落盘路径。与 D 的数据库目录分开，避免覆盖队友的表结构。
DEFAULT_DB_PATH = Path("data/campusflow_b.db")


def build_repository(storage: Optional[str] = None,
                     db_path: Optional[Path | str] = None,
                     env: Optional[Mapping[str, str]] = None) -> CampusRepository:
    """按配置造出仓储实现。

    ``storage`` 显式传入时优先；否则读 ``CPAFFAIRS_STORAGE``；默认 ``sqlite``。
    """
    source = os.environ if env is None else env
    mode = (storage or source.get("CPAFFAIRS_STORAGE") or "sqlite").strip().lower()
    if mode == "memory":
        return InMemoryRepository()
    if mode != "sqlite":
        raise ValueError("存储方式仅支持 sqlite 或 memory，收到：%s" % mode)
    path = db_path or source.get("CPAFFAIRS_DB") or DEFAULT_DB_PATH
    return SqlRepository(path)


@dataclass
class BToolBundle:
    """B 侧装配结果，保留各组件引用便于测试与后续注入。"""

    knowledge_tools: KnowledgeTools
    affair_tools: AffairTools
    dormitory_tools: DormitoryTools
    repository: CampusRepository
    knowledge_base: KnowledgeBase


def build_bundle(*, repository: Optional[CampusRepository] = None,
                 knowledge_base: Optional[KnowledgeBase] = None,
                 clock=None,
                 storage: Optional[str] = None,
                 db_path: Optional[Path | str] = None) -> BToolBundle:
    """装配 B 侧服务与工具。

    ``repository`` 与 ``knowledge_base`` 均可注入：
    - 单测注入内存实现与临时索引，不碰真实文件。
    - 集成时由 D 注入其数据库实现。
    - 不注入时按 ``storage`` / 环境变量造仓储，默认 SQLite 落盘。
    """
    repo = repository or build_repository(storage=storage, db_path=db_path)
    kb = knowledge_base or build_knowledge_base()
    return BToolBundle(
        knowledge_tools=KnowledgeTools(kb),
        affair_tools=AffairTools(AffairService(repo, clock=clock)),
        dormitory_tools=DormitoryTools(DormitoryService(repo, clock=clock)),
        repository=repo,
        knowledge_base=kb,
    )


def bind_b_tools(registry, bundle: Optional[BToolBundle] = None, *,
                 repository: Optional[CampusRepository] = None,
                 knowledge_base: Optional[KnowledgeBase] = None,
                 clock=None,
                 storage: Optional[str] = None,
                 db_path: Optional[Path | str] = None) -> BToolBundle:
    """把 B 的 5 个工具绑定到 A 的 registry，返回装配结果。"""
    resolved = bundle or build_bundle(
        repository=repository, knowledge_base=knowledge_base, clock=clock,
        storage=storage, db_path=db_path,
    )
    resolved.knowledge_tools.bind(registry)
    resolved.affair_tools.bind(registry)
    resolved.dormitory_tools.bind(registry)
    return resolved


def build_knowledge_index(sources_dir: Path = Path("data/knowledge"),
                          knowledge_base: Optional[KnowledgeBase] = None) -> int:
    """把 ``data/knowledge/`` 下的文档写入索引，返回片段数。

    CLI 入口见 ``python -m backend.rag.cli rebuild``。
    """
    kb = knowledge_base or build_knowledge_base()
    return kb.build_from_directory(sources_dir)
