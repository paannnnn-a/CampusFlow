"""B 侧 SQLite 仓储实现。

**为什么需要这个文件**

CampusFlow 的 B 交付验收要求「事务草稿和报修记录真正写入数据库，能通过 ID 查询」。
D 的 ``backend/database/models.py`` 统一模型尚未落地，但内存实现无法满足验收，
因此 B 先提供只覆盖**自己三张表**的 SQLite 实现，等 D 的统一模型就绪后迁移。

**职责边界**

- 本文件只建 B 需要的表：``affairs_requests``、``repair_tickets``、
  ``repair_ticket_history``、``tool_operations``。
- 不碰 D 负责的 ``users`` / ``courses`` / ``rooms`` / ``seats`` / ``reservations`` /
  ``activities`` / ``conversations`` / ``messages``。
- 表名与分工文档 4.1 一致，字段命名贴近 D 后续可能采用的风格，降低迁移成本。

**真正的数据库级保证**（内存实现做不到的部分）

1. ``tool_operations`` 的 ``(user_id, tool_name, operation_id)`` 唯一索引：
   多进程/重启后依然能防重复写入，这是 tool_contract 第 2 条的要求。
2. ``repair_tickets`` 的部分唯一索引 ``(user_id, location, problem)``
   限定在未完结状态：数据库层面挡住重复报修，不依赖应用层判断。
3. 写操作在 ``BEGIN IMMEDIATE`` 事务内完成查重与写入，
   避免「先查后写」之间的竞态。

**字段长度与校验**

业务层已完成字段校验（见 ``services/dormitory.py`` 的位置/故障规则），
数据库再设 NOT NULL 与非空 CHECK 兜底，防止绕过 service 直接写库。
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional

from .b_repository import (
    AffairDraftRecord,
    CampusRepository,
    DuplicateOperation,
    RepairTicketRecord,
    fingerprint,
)

#: 未完结工单状态，用于部分唯一索引的谓词。
OPEN_STATUSES = ("pending", "accepted", "processing")

SCHEMA = """
CREATE TABLE IF NOT EXISTS affairs_requests (
    draft_id      TEXT PRIMARY KEY,
    user_id       TEXT NOT NULL,
    affair_type   TEXT NOT NULL,
    fields_json   TEXT NOT NULL,
    status        TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    CHECK (length(trim(user_id)) > 0),
    CHECK (length(trim(affair_type)) > 0),
    CHECK (length(trim(status)) > 0)
);

CREATE INDEX IF NOT EXISTS idx_affairs_user ON affairs_requests(user_id);
CREATE INDEX IF NOT EXISTS idx_affairs_type ON affairs_requests(affair_type);

CREATE TABLE IF NOT EXISTS repair_tickets (
    ticket_id   TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    location    TEXT NOT NULL,
    problem     TEXT NOT NULL,
    status      TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    CHECK (length(trim(user_id)) > 0),
    CHECK (length(trim(location)) > 1),
    CHECK (length(trim(problem)) > 1),
    CHECK (status IN ('pending', 'accepted', 'processing', 'resolved', 'cancelled'))
);

CREATE INDEX IF NOT EXISTS idx_tickets_user ON repair_tickets(user_id);

-- 同一用户、同一位置、同一故障，在未完结状态下只允许一条工单。
-- 这是独立于幂等键的业务唯一约束（tool_contract 第 3 条）。
CREATE UNIQUE INDEX IF NOT EXISTS uq_open_ticket
    ON repair_tickets(user_id, location, problem)
    WHERE status IN ('pending', 'accepted', 'processing');

CREATE TABLE IF NOT EXISTS repair_ticket_history (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id  TEXT NOT NULL,
    status     TEXT NOT NULL,
    at         TEXT NOT NULL,
    note       TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (ticket_id) REFERENCES repair_tickets(ticket_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_history_ticket ON repair_ticket_history(ticket_id);

-- 幂等键：同一 (user_id, tool_name, operation_id) 只允许一条，
-- 参数指纹不同则拒绝，保证「换个操作号不能重复占位」的另一半语义。
CREATE TABLE IF NOT EXISTS tool_operations (
    user_id      TEXT NOT NULL,
    tool_name    TEXT NOT NULL,
    operation_id TEXT NOT NULL,
    args_hash    TEXT NOT NULL,
    result_json  TEXT,
    created_at   TEXT NOT NULL,
    PRIMARY KEY (user_id, tool_name, operation_id),
    CHECK (length(trim(operation_id)) > 0)
);
"""


class SqlRepository(CampusRepository):
    """SQLite 实现。每个方法使用独立连接，适合单机演示与 FastAPI 线程池。"""

    def __init__(self, path: Path | str = "data/campusflow.db", *,
                 clock=None, timeout: float = 10.0):
        self.path = Path(path)
        self._clock = clock or (lambda: datetime.now().astimezone())
        self.timeout = timeout
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._memory_conn: Optional[sqlite3.Connection] = None
        self._initialized = False
        self._init_schema()

    # ------------------------------------------------------------ 连接

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        if str(self.path) == ":memory:":
            # 内存库必须复用同一连接，否则每次都是空库。
            if self._memory_conn is None:
                self._memory_conn = sqlite3.connect(
                    ":memory:", timeout=self.timeout,
                    detect_types=sqlite3.PARSE_DECLTYPES,
                )
                self._memory_conn.row_factory = sqlite3.Row
                self._memory_conn.execute("PRAGMA foreign_keys = ON")
            yield self._memory_conn
            return

        conn = sqlite3.connect(str(self.path), timeout=self.timeout)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            # WAL 提升并发读性能，单机演示足够。
            conn.execute("PRAGMA journal_mode = WAL")
            yield conn
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(SCHEMA)
            conn.commit()
        self._initialized = True

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        """写事务：BEGIN IMMEDIATE 立即取写锁，避免查重与写入之间被并发插入。"""
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except Exception:
                conn.rollback()
                raise
            else:
                conn.commit()

    # ------------------------------------------------------------ 幂等

    def begin_operation(self, *, user_id, tool_name, operation_id, arguments):
        digest = fingerprint(dict(arguments))
        now = self._clock().isoformat()
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT args_hash, result_json FROM tool_operations"
                " WHERE user_id = ? AND tool_name = ? AND operation_id = ?",
                (user_id, tool_name, operation_id),
            ).fetchone()

            if row is not None:
                if row["args_hash"] != digest:
                    raise DuplicateOperation("同一操作号不能用于不同参数")
                cached = row["result_json"]
                return json.loads(cached) if cached else None

            # 先占位：并发下第二个请求会撞主键，由唯一约束兜底。
            conn.execute(
                "INSERT INTO tool_operations"
                " (user_id, tool_name, operation_id, args_hash, result_json, created_at)"
                " VALUES (?, ?, ?, ?, NULL, ?)",
                (user_id, tool_name, operation_id, digest, now),
            )
            return None

    def finish_operation(self, *, user_id, tool_name, operation_id, data):
        payload = json.dumps(dict(data), ensure_ascii=False, sort_keys=True)
        with self._transaction() as conn:
            conn.execute(
                "UPDATE tool_operations SET result_json = ?"
                " WHERE user_id = ? AND tool_name = ? AND operation_id = ?",
                (payload, user_id, tool_name, operation_id),
            )

    # ------------------------------------------------------------ 事务草稿

    def save_affair_draft(self, record: AffairDraftRecord) -> None:
        with self._transaction() as conn:
            conn.execute(
                "INSERT INTO affairs_requests"
                " (draft_id, user_id, affair_type, fields_json, status, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (record.draft_id, record.user_id, record.affair_type,
                 json.dumps(record.fields, ensure_ascii=False, sort_keys=True),
                 record.status, record.created_at.isoformat()),
            )

    def get_affair_draft(self, draft_id: str) -> Optional[AffairDraftRecord]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM affairs_requests WHERE draft_id = ?", (draft_id,),
            ).fetchone()
        return self._to_draft(row) if row else None

    def list_affair_drafts(self, user_id: str) -> List[AffairDraftRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM affairs_requests WHERE user_id = ? ORDER BY created_at DESC",
                (user_id,),
            ).fetchall()
        return [self._to_draft(row) for row in rows]

    @staticmethod
    def _to_draft(row: sqlite3.Row) -> AffairDraftRecord:
        return AffairDraftRecord(
            draft_id=row["draft_id"],
            affair_type=row["affair_type"],
            fields=json.loads(row["fields_json"]),
            status=row["status"],
            user_id=row["user_id"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    # ------------------------------------------------------------ 报修工单

    def create_repair_ticket(self, record: RepairTicketRecord) -> None:
        with self._transaction() as conn:
            try:
                conn.execute(
                    "INSERT INTO repair_tickets"
                    " (ticket_id, user_id, location, problem, status, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (record.ticket_id, record.user_id, record.location, record.problem,
                     record.status, record.created_at.isoformat(),
                     record.updated_at.isoformat()),
                )
            except sqlite3.IntegrityError as exc:
                # 部分唯一索引触发：已有未完结的同位置同故障工单。
                if "uq_open_ticket" in str(exc) or "UNIQUE" in str(exc).upper():
                    raise DuplicateOperation("该位置同一故障已有未完结工单") from exc
                raise
            for item in record.history:
                conn.execute(
                    "INSERT INTO repair_ticket_history (ticket_id, status, at, note)"
                    " VALUES (?, ?, ?, ?)",
                    (record.ticket_id, item["status"], item["at"], item.get("note", "")),
                )

    def get_repair_ticket(self, ticket_id: str) -> Optional[RepairTicketRecord]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM repair_tickets WHERE ticket_id = ?", (ticket_id,),
            ).fetchone()
            if row is None:
                return None
            history = conn.execute(
                "SELECT status, at, note FROM repair_ticket_history"
                " WHERE ticket_id = ? ORDER BY id",
                (ticket_id,),
            ).fetchall()
        return self._to_ticket(row, history)

    def list_repair_tickets(self, user_id: str) -> List[RepairTicketRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM repair_tickets WHERE user_id = ? ORDER BY created_at DESC",
                (user_id,),
            ).fetchall()
            history_map: Dict[str, List[sqlite3.Row]] = {}
            if rows:
                ids = [row["ticket_id"] for row in rows]
                placeholders = ",".join("?" * len(ids))
                for item in conn.execute(
                    "SELECT ticket_id, status, at, note FROM repair_ticket_history"
                    " WHERE ticket_id IN (%s) ORDER BY id" % placeholders,
                    ids,
                ):
                    history_map.setdefault(item["ticket_id"], []).append(item)
        return [self._to_ticket(row, history_map.get(row["ticket_id"], [])) for row in rows]

    def update_repair_ticket(self, record: RepairTicketRecord) -> None:
        with self._transaction() as conn:
            conn.execute(
                "UPDATE repair_tickets SET status = ?, updated_at = ?,"
                " location = ?, problem = ? WHERE ticket_id = ?",
                (record.status, record.updated_at.isoformat(), record.location,
                 record.problem, record.ticket_id),
            )
            # 历史表只追加：只插入本次新增的最后一条，避免重复写入整段历史。
            if record.history:
                last = record.history[-1]
                exists = conn.execute(
                    "SELECT 1 FROM repair_ticket_history"
                    " WHERE ticket_id = ? AND at = ? AND status = ?",
                    (record.ticket_id, last["at"], last["status"]),
                ).fetchone()
                if not exists:
                    conn.execute(
                        "INSERT INTO repair_ticket_history (ticket_id, status, at, note)"
                        " VALUES (?, ?, ?, ?)",
                        (record.ticket_id, last["status"], last["at"], last.get("note", "")),
                    )

    def find_open_ticket(self, *, user_id, location, problem):
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM repair_tickets"
                " WHERE user_id = ? AND location = ? AND problem = ?"
                "   AND status IN ('pending', 'accepted', 'processing')",
                (user_id, location, problem),
            ).fetchone()
            if row is None:
                return None
            history = conn.execute(
                "SELECT status, at, note FROM repair_ticket_history"
                " WHERE ticket_id = ? ORDER BY id",
                (row["ticket_id"],),
            ).fetchall()
        return self._to_ticket(row, history)

    @staticmethod
    def _to_ticket(row: sqlite3.Row,
                   history: List[sqlite3.Row]) -> RepairTicketRecord:
        return RepairTicketRecord(
            ticket_id=row["ticket_id"],
            location=row["location"],
            problem=row["problem"],
            status=row["status"],
            user_id=row["user_id"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            history=[
                {"status": item["status"], "at": item["at"], "note": item["note"]}
                for item in history
            ],
        )

    # ------------------------------------------------------------ 运维

    def reset(self) -> None:
        """清空 B 侧全部数据。演示数据重置用。"""
        with self._transaction() as conn:
            for table in ("repair_ticket_history", "repair_tickets",
                          "affairs_requests", "tool_operations"):
                conn.execute("DELETE FROM %s" % table)

    def counts(self) -> Mapping[str, int]:
        with self._connect() as conn:
            return {
                table: conn.execute("SELECT COUNT(*) AS n FROM %s" % table).fetchone()["n"]
                for table in ("affairs_requests", "repair_tickets", "tool_operations")
            }
