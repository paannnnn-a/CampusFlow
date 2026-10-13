from typing import Any, Dict, List, Optional
from typing_extensions import TypedDict


class AgentState(TypedDict):
    messages: List[Dict[str, Any]]
    turn_start: int
    user_id: str
    now: str
    calls: List[Dict[str, Any]]
    pointer: int
    current: Optional[Dict[str, Any]]
    pending: Optional[Dict[str, Any]]
    approved: bool
    approval_expired: bool
    rounds: int
    tool_count: int
    trace: List[Dict[str, Any]]
    status: str
    answer: str

