from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, Type

from pydantic import BaseModel

from .contracts import (
    ActivityArgs, ActivitySearchArgs, AffairArgs, BookArgs, Context, CourseScheduleArgs, DateArgs,
    DraftArgs, EmptyClassroomArgs, FreeSlotArgs, FreeTimeArgs, IntervalArgs, KnowledgeArgs, RegistrationArgs,
    RepairArgs, ReservationArgs, TicketArgs, ToolResult,
)


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    args_type: Type[BaseModel]
    write: bool = False


SPECS = (
    ToolSpec("get_course_schedule", "按用户提供的学号查询课表，返回 weekday 和 course_list。不得猜测学号，也不得把单个 weekday 的结果当作整周课表。", CourseScheduleArgs),
    ToolSpec("analyze_free_time", "按用户提供的学号分析空闲时间，返回 free_periods，每项含 weekday 和 time_range。结果只包含星期与时段，不代表某个具体日期或教学周。", FreeTimeArgs),
    ToolSpec("get_empty_classroom", "按星期几及校园当地 HH:MM 起止时间查询空教室，返回 classroom_list。weekday 为 1（周一）至 7（周日）。空教室查询不代表可预约座位或预约成功。", EmptyClassroomArgs),
    ToolSpec("get_schedule", "查询当前用户一天的课表。", DateArgs),
    ToolSpec("find_free_slots", "计算当天 08:00–22:00 内可连续学习的空闲时间，须按用户指定上午/下午筛选。", FreeSlotArgs),
    ToolSpec("find_available_seats", "查询指定时段可用的座位。", IntervalArgs),
    ToolSpec("book_study_seat", "模拟预约座位；执行器会请求确认。", BookArgs, True),
    ToolSpec("cancel_study_reservation", "取消本人模拟预约；执行器会请求确认。", ReservationArgs, True),
    ToolSpec("search_campus_knowledge", "校园政策、办事材料和流程的第一步：检索知识依据和来源。之后再查询结构化办理要求，答复引用 source_id/title。", KnowledgeArgs),
    ToolSpec("get_affair_requirements", "事务办理第二步：先检索对应校园知识，再查询材料、必填字段和流程。本工具不代替知识依据。", AffairArgs),
    ToolSpec("create_affair_draft", "先完成知识检索和办理要求查询、补齐用户必填信息，再保存本人模拟草稿；执行器会请求确认。", DraftArgs, True),
    ToolSpec("search_activities", "按关键词和日期范围检索模拟活动；报名之前还要查课表排除冲突。", ActivitySearchArgs),
    ToolSpec("register_activity", "模拟报名活动；执行器会请求确认。", ActivityArgs, True),
    ToolSpec("cancel_activity_registration", "取消本人模拟报名；执行器会请求确认。", RegistrationArgs, True),
    ToolSpec("create_repair_ticket", "创建模拟报修工单；位置和故障必填，执行器会请求确认。", RepairArgs, True),
    ToolSpec("get_repair_status", "查询本人模拟工单状态。", TicketArgs),
)

PREREQUISITES = {
    "get_affair_requirements": {"search_campus_knowledge"},
    "create_affair_draft": {"search_campus_knowledge", "get_affair_requirements"},
}


def allowed_tools(completed):
    return {spec.name for spec in SPECS if PREREQUISITES.get(spec.name, set()).issubset(completed)}


class ToolRegistry:
    def __init__(self):
        self.specs = {spec.name: spec for spec in SPECS}
        self._handlers: Dict[str, Callable] = {}
        self._simulated: Dict[str, bool] = {}

    def bind(self, name: str, handler: Callable, *, simulated: bool):
        """替换函数，不允许业务适配器改变写操作分类。启动时绑定。"""
        if name not in self.specs:
            raise KeyError(name)
        self._handlers[name] = handler
        self._simulated[name] = simulated

    def is_simulated(self, name):
        return self._simulated.get(name, True)

    def invoke(self, name, arguments, context: Context) -> ToolResult:
        # Python handler(args: Pydantic, context: Context) -> ToolResult | dict
        args = self.specs[name].args_type.model_validate(arguments)
        raw = self._handlers[name](args, context)
        result = ToolResult.model_validate(raw)
        return result.model_copy(update={"simulated": self.is_simulated(name)})

    def schemas(self, allowed=None):
        return [{"type": "function", "function": {
            "name": s.name, "description": s.description,
            "parameters": s.args_type.model_json_schema(),
        }} for s in self.specs.values() if s.name in self._handlers and (allowed is None or s.name in allowed)]

    def ready(self, name):
        return name in self._handlers

