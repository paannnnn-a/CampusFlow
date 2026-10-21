"""B 侧业务数据访问接口。

**职责边界说明（重要）**

CampusFlow 规定 D 负责 ``backend/database/`` 的表结构、迁移与初始化，
B 负责业务规则与读写。D 的数据库层尚未落地，因此这里只定义 B 需要的
**访问接口**（与表无关的抽象），并提供两种实现：

- ``InMemoryRepository``：单元测试与无数据库环境的降级实现。
- 后续 D 提供 SQLAlchemy 模型后，新增一个 ``SqlRepository`` 实现同一接口即可，
  业务层与工具层无需改动。

B 需要的表（对应分工文档 4.1）：

- ``affairs_requests``：事务申请草稿
- ``repair_tickets``：报修工单
- ``tool_operations``：B/C 共用的幂等键记录 ``(user_id, tool_name, operation_id)``

**幂等约定**（对应 tool_contract「必须由 B/C 保证的写入规则」第 2、3、4 条）：
1. 相同 ``(user_id, tool_name, operation_id)`` 且参数一致 → 返回原结果，不重复写入。
2. 相同幂等键但参数变化 → 拒绝，返回 ``IDEMPOTENCY_KEY_REUSED``，不生成新记录。
3. 业务唯一约束独立于操作号，由具体服务实现检查（如「同一用户同一工单不重复」）。
"""
from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from threading import RLock
from typing import Any, Dict, List, Mapping, Optional


class RepositoryError(RuntimeError):
    """仓储层错误。"""


class DuplicateOperation(RepositoryError):
    """同一幂等键被用于不同参数。"""


@dataclass
class AffairDraftRecord:
    draft_id: str
    affair_type: str
    fields: Dict[str, str]
    status: str
    user_id: str
    created_at: datetime

    def to_public(self) -> Dict[str, Any]:
        """对外结构：不含 user_id，时间统一带时区 ISO 8601。"""
        return {
            "draft_id": self.draft_id,
            "affair_type": self.affair_type,
            "fields": dict(self.fields),
            "status": self.status,
            "created_at": self.created_at.isoformat(),
        }


#: 报修工单允许的状态流转。只允许向前推进与业主取消，避免演示数据出现回退。
REPAIR_TRANSITIONS: Dict[str, frozenset] = {
    "pending": frozenset({"accepted", "cancelled"}),
    "accepted": frozenset({"processing", "cancelled"}),
    "processing": frozenset({"resolved"}),
    "resolved": frozenset(),
    "cancelled": frozenset(),
}


@dataclass
class RepairTicketRecord:
    ticket_id: str
    location: str
    problem: str
    status: str
    user_id: str
    created_at: datetime
    updated_at: datetime
    history: List[Dict[str, str]] = field(default_factory=list)

    def to_public(self, *, include_history: bool = True) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "ticket_id": self.ticket_id,
            "location": self.location,
            "problem": self.problem,
            "status": self.status,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }
        if include_history:
            data["history"] = [dict(item) for item in self.history]
        return data


def fingerprint(payload: Mapping[str, Any]) -> str:
    """对参数做稳定指纹，用于判断「同一幂等键是否更换了参数」。"""
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class CampusRepository(ABC):
    """B 侧业务数据访问接口。写操作必须在事务内完成查重与写入。"""

    # ------------------------------------------------------------ 幂等

    @abstractmethod
    def begin_operation(self, *, user_id: str, tool_name: str,
                        operation_id: str, arguments: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
        """登记一次写操作。

        返回已缓存的 ``data`` 表示这是重复调用，业务层应直接返回原结果；
        返回 ``None`` 表示首次调用，可以继续执行业务逻辑。
        同键不同参数时抛 ``DuplicateOperation``。
        """

    @abstractmethod
    def finish_operation(self, *, user_id: str, tool_name: str,
                         operation_id: str, data: Mapping[str, Any]) -> None:
        """记录成功结果，供重复调用返回。"""

    # ------------------------------------------------------------ 事务草稿

    @abstractmethod
    def save_affair_draft(self, record: AffairDraftRecord) -> None:
        """保存草稿。"""

    @abstractmethod
    def get_affair_draft(self, draft_id: str) -> Optional[AffairDraftRecord]:
        """按编号取草稿；不存在返回 None。"""

    @abstractmethod
    def list_affair_drafts(self, user_id: str) -> List[AffairDraftRecord]:
        """取某用户的全部草稿，供记录页展示。"""

    # ------------------------------------------------------------ 报修工单

    @abstractmethod
    def create_repair_ticket(self, record: RepairTicketRecord) -> None:
        """创建工单。同一用户同一时间同位置同问题的重复工单由业务层拦截。"""

    @abstractmethod
    def get_repair_ticket(self, ticket_id: str) -> Optional[RepairTicketRecord]:
        """按编号取工单；不存在返回 None。"""

    @abstractmethod
    def list_repair_tickets(self, user_id: str) -> List[RepairTicketRecord]:
        """取某用户的全部工单。"""

    @abstractmethod
    def update_repair_ticket(self, record: RepairTicketRecord) -> None:
        """更新工单状态与历史。"""

    @abstractmethod
    def find_open_ticket(self, *, user_id: str, location: str, problem: str) -> Optional[RepairTicketRecord]:
        """查找同一用户未完结的相同工单，用于防重复提交。"""


class InMemoryRepository(CampusRepository):
    """进程内实现。加锁保证单进程并发安全；重启后数据丢失。

    这不是最终方案：D 的数据库落地后应替换为 SQL 实现，
    并依靠数据库唯一索引保证多进程下的幂等与防重。
    """

    def __init__(self):
        self._lock = RLock()
        self._operations: Dict[tuple, tuple] = {}
        self._drafts: Dict[str, AffairDraftRecord] = {}
        self._tickets: Dict[str, RepairTicketRecord] = {}

    def begin_operation(self, *, user_id, tool_name, operation_id, arguments):
        key = (user_id, tool_name, operation_id)
        digest = fingerprint(dict(arguments))
        with self._lock:
            existing = self._operations.get(key)
            if existing is None:
                # 先占位，避免并发下重复执行；结果由 finish_operation 补写。
                self._operations[key] = (digest, None)
                return None
            stored_digest, stored_data = existing
            if stored_digest != digest:
                raise DuplicateOperation("同一操作号不能用于不同参数")
            return dict(stored_data) if stored_data is not None else None

    def finish_operation(self, *, user_id, tool_name, operation_id, data):
        key = (user_id, tool_name, operation_id)
        with self._lock:
            existing = self._operations.get(key)
            digest = existing[0] if existing else fingerprint({})
            self._operations[key] = (digest, dict(data))

    def save_affair_draft(self, record):
        with self._lock:
            self._drafts[record.draft_id] = record

    def get_affair_draft(self, draft_id):
        with self._lock:
            return self._drafts.get(draft_id)

    def list_affair_drafts(self, user_id):
        with self._lock:
            return [r for r in self._drafts.values() if r.user_id == user_id]

    def create_repair_ticket(self, record):
        with self._lock:
            self._tickets[record.ticket_id] = record

    def get_repair_ticket(self, ticket_id):
        with self._lock:
            return self._tickets.get(ticket_id)

    def list_repair_tickets(self, user_id):
        with self._lock:
            return [r for r in self._tickets.values() if r.user_id == user_id]

    def update_repair_ticket(self, record):
        with self._lock:
            self._tickets[record.ticket_id] = record

    def find_open_ticket(self, *, user_id, location, problem):
        with self._lock:
            for record in self._tickets.values():
                if (record.user_id == user_id and record.location == location
                        and record.problem == problem
                        and record.status in {"pending", "accepted", "processing"}):
                    return record
        return None
