"""D 调用的稳定入口。单进程会话存储；认证由 D 提供。"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import json
from threading import RLock
from uuid import uuid4

from langgraph.types import Command

from .contracts import AgentResponse, CAMPUS_TZ, PendingConfirmation
from .graph import build_graph
from .mock_tools import MockCampus
from .planner import DemoPlanner


class AgentService:
    def __init__(self, model, registry, *, clock=None, **graph_options):
        self.clock = clock or (lambda: datetime.now(CAMPUS_TZ))
        self.graph = build_graph(model, registry, clock=self.clock, **graph_options)
        self.registry = registry
        self._sessions = {}
        self._lock = RLock()

    @classmethod
    def demo(cls, *, clock=None, **options):
        clock = clock or (lambda: datetime.now(CAMPUS_TZ))
        campus = MockCampus(now=clock())
        service = cls(DemoPlanner(), campus.registry(), clock=clock, **options)
        service.mock_campus = campus
        return service

    def _session(self, user_id, session_id, *, create=False):
        if not isinstance(user_id, str) or not user_id.strip():
            raise ValueError("需要可信 user_id")
        with self._lock:
            if session_id is None and create:
                session_id = uuid4().hex
                self._sessions[session_id] = (user_id, RLock())
            session = self._sessions.get(session_id)
            if not session or session[0] != user_id:
                raise ValueError("会话不存在或不属于当前用户")
        return session_id, session[1]

    @staticmethod
    def _config(session_id):
        return {"configurable": {"thread_id": session_id}, "recursion_limit": 300}

    def chat(self, *, user_id, message, session_id=None):
        if not isinstance(message, str) or not message.strip() or len(message) > 8000:
            raise ValueError("消息须为 1–8000 个字符")
        session_id, lock = self._session(user_id, session_id, create=True)
        with lock:
            config = self._config(session_id)
            old = self.graph.get_state(config).values
            if old.get("pending"):
                raise ValueError("请先确认或取消待执行操作；修改参数时先取消，再发送新请求")
            messages = old.get("messages", [])
            state = {"messages": messages + [{"role": "user", "content": message.strip()}],
                     "turn_start": len(messages), "user_id": user_id, "now": self.clock().isoformat(),
                     "calls": [], "pointer": 0, "current": None, "pending": None, "approved": False, "approval_expired": False,
                     "rounds": 0, "tool_count": 0, "trace": [], "status": "running", "answer": ""}
            self.graph.invoke(state, config)
            return self._response(session_id)

    def confirm(self, *, user_id, session_id, confirmation_id, approved):
        if type(approved) is not bool:
            raise ValueError("approved 必须是布尔值")
        session_id, lock = self._session(user_id, session_id)
        with lock:
            config = self._config(session_id)
            snapshot = self.graph.get_state(config)
            raw_pending = snapshot.values.get("pending")
            if not raw_pending or not snapshot.next:
                raise ValueError("没有待确认操作；此确认可能已使用")
            pending = PendingConfirmation.model_validate(raw_pending)
            if confirmation_id != pending.confirmation_id:
                raise ValueError("确认编号过期或不匹配")
            expired = self.clock() >= pending.expires_at
            self.graph.invoke(Command(resume={"confirmation_id": confirmation_id, "approved": approved and not expired}), config)
            return self._response(session_id)

    def inspect(self, *, user_id, session_id):
        session_id, lock = self._session(user_id, session_id)
        with lock:
            return self._response(session_id)

    def history(self, *, user_id, session_id):
        session_id, lock = self._session(user_id, session_id)
        with lock:
            return deepcopy(self.graph.get_state(self._config(session_id)).values.get("messages", []))

    def _response(self, session_id):
        state = self.graph.get_state(self._config(session_id)).values
        pending = state.get("pending")
        answer = state.get("answer", "")
        if pending:
            answer = "请核对以下%s操作，确认后执行：%s\n%s" % (
                "模拟" if pending["simulated"] else "业务", pending["tool_name"],
                json.dumps(pending["arguments"], ensure_ascii=False))
        elif any(t["result"]["simulated"] for t in state.get("trace", [])):
            answer = "【含模拟数据】\n" + answer
        return AgentResponse(session_id=session_id, status="awaiting_confirmation" if pending else state["status"],
                             answer=answer, pending_confirmation=pending, trace=state.get("trace", []))
