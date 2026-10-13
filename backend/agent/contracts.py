"""A 的候选协议；联调后由 D 统一公共 Schema，不覆盖 D 的目录。"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, List, Literal, Optional
from zoneinfo import ZoneInfo

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictBool, model_validator, field_validator

CAMPUS_TZ = ZoneInfo("Asia/Shanghai")


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Context(Contract):
    """由认证层构造，绝不从模型参数提取。"""
    user_id: str = Field(min_length=1, max_length=100)
    operation_id: str = Field(min_length=1)


class ToolResult(Contract):
    success: StrictBool
    data: Dict[str, Any] = Field(default_factory=dict)
    message: str
    error_code: Optional[str] = None
    simulated: StrictBool = True
    retryable: StrictBool = False

    @model_validator(mode="after")
    def consistent(self):
        if self.success and (self.error_code or self.retryable):
            raise ValueError("成功结果不能含错误码或 retryable=true")
        if not self.success and not self.error_code:
            raise ValueError("失败结果必须含 error_code")
        return self


def ok(data=None, message="模拟操作成功"):
    return ToolResult(success=True, data=data or {}, message=message)


def fail(code, message, *, retryable=False):
    return ToolResult(success=False, message=message, error_code=code, retryable=retryable)


class DateArgs(Contract):
    date: str = Field(description="校园日期 YYYY-MM-DD")

    @field_validator("date")
    @classmethod
    def valid_date(cls, value):
        if date.fromisoformat(value).isoformat() != value:
            raise ValueError("日期必须为 YYYY-MM-DD")
        return value


class FreeSlotArgs(DateArgs):
    duration_minutes: int = Field(ge=15, le=480, strict=True)


class IntervalArgs(Contract):
    start_time: AwareDatetime = Field(description="含时区 ISO 8601，例如 2026-10-14T14:00:00+08:00")
    end_time: AwareDatetime

    @field_validator("start_time", "end_time", mode="before")
    @classmethod
    def require_iso(cls, value):
        if not isinstance(value, (str, datetime)):
            raise ValueError("时间必须是带时区 ISO 8601 字符串")
        return value

    @field_validator("start_time", "end_time")
    @classmethod
    def normalize(cls, value):
        return value.astimezone(CAMPUS_TZ)

    @model_validator(mode="after")
    def ordered(self):
        if self.end_time <= self.start_time:
            raise ValueError("结束时间必须晚于开始时间")
        return self


class BookArgs(IntervalArgs):
    seat_id: str = Field(min_length=1, max_length=100)


class ReservationArgs(Contract):
    reservation_id: str = Field(min_length=1, max_length=100)


class KnowledgeArgs(Contract):
    query: str = Field(min_length=1, max_length=1000)


class AffairArgs(Contract):
    affair_type: str = Field(min_length=1, max_length=100)


class DraftArgs(AffairArgs):
    fields: Dict[str, str] = Field(description="用户提供的申请字段；不得猜测姓名、学号")


class ActivitySearchArgs(IntervalArgs):
    keywords: str = Field(min_length=1, max_length=100)


class ActivityArgs(Contract):
    activity_id: str = Field(min_length=1, max_length=100)


class RegistrationArgs(Contract):
    registration_id: str = Field(min_length=1, max_length=100)


class RepairArgs(Contract):
    location: str = Field(min_length=2, max_length=200)
    problem: str = Field(min_length=2, max_length=1000)


class TicketArgs(Contract):
    ticket_id: str = Field(min_length=1, max_length=100)


class PendingConfirmation(Contract):
    confirmation_id: str
    tool_name: str
    arguments: Dict[str, Any]
    expires_at: datetime
    simulated: bool
    message: str = "请核对工具名称和全部参数，确认后才执行。"


class Trace(Contract):
    tool_name: str
    call_id: str
    operation_id: str
    arguments: Dict[str, Any]
    result: ToolResult
    attempts: int
    duration_ms: float


class AgentResponse(Contract):
    session_id: str
    status: Literal["completed", "awaiting_confirmation", "cancelled", "failed"]
    answer: str
    pending_confirmation: Optional[PendingConfirmation] = None
    trace: List[Trace] = Field(default_factory=list)

