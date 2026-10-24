"""C 侧契约：模拟业务处理器验证绑定、参数边界与完整 Agent 调用链。"""
import json
from datetime import datetime

import pytest
from pydantic import ValidationError

from backend.agent.contracts import (
    Context, CourseScheduleArgs, EmptyClassroomArgs, FreeTimeArgs, ok,
)
from backend.agent.executor import Executor
from backend.agent.mock_tools import MockCampus
from backend.agent.planner import call, say
from backend.agent.service import AgentService
from backend.agent.tool_registry import ToolRegistry


NOW = datetime.fromisoformat("2026-10-24T09:00:00+08:00")
TOOL_NAMES = ("get_course_schedule", "analyze_free_time", "get_empty_classroom")


class TimeSpaceToolsStub:
    """仅验证协议；并非 C 的真实课表或空教室实现。"""
    def __init__(self):
        self.calls = []

    def get_course_schedule(self, args, context):
        assert isinstance(args, CourseScheduleArgs)
        self.calls.append(("get_course_schedule", args, context))
        return ok({"weekday": 1, "course_list": [
            {"time": "08:00-09:40", "course_name": "高等数学", "location": "教1-203"},
            {"time": "10:00-11:40", "course_name": "大学英语", "location": "教2-105"},
        ]}, "模拟课表")

    def analyze_free_time(self, args, context):
        assert isinstance(args, FreeTimeArgs)
        self.calls.append(("analyze_free_time", args, context))
        return ok({"free_periods": [
            {"weekday": 2, "time_range": "14:00-18:00"},
            {"weekday": 4, "time_range": "19:00-22:00"},
        ]}, "模拟空闲时间")

    def get_empty_classroom(self, args, context):
        assert isinstance(args, EmptyClassroomArgs)
        self.calls.append(("get_empty_classroom", args, context))
        return ok({"classroom_list": [
            {"building": "教3", "room": "402"}, {"building": "教3", "room": "405"},
        ]}, "模拟空教室")


def bind_to_registry(registry):
    ts_tool = TimeSpaceToolsStub()
    registry.bind("get_course_schedule", ts_tool.get_course_schedule, simulated=True)
    registry.bind("analyze_free_time", ts_tool.analyze_free_time, simulated=True)
    registry.bind("get_empty_classroom", ts_tool.get_empty_classroom, simulated=True)
    return ts_tool


def test_c_binding_exposes_schemas_without_replacing_existing_mocks():
    registry = MockCampus(now=NOW).registry()
    before = {schema["function"]["name"] for schema in registry.schemas()}
    assert len(before) == 13
    assert not before.intersection(TOOL_NAMES)
    bind_to_registry(registry)
    schemas = {schema["function"]["name"]: schema["function"]["parameters"]
               for schema in registry.schemas()}
    assert set(schemas) == before.union(TOOL_NAMES)
    assert schemas["get_course_schedule"]["required"] == ["student_id"]
    assert schemas["analyze_free_time"]["required"] == ["student_id"]
    assert set(schemas["get_empty_classroom"]["required"]) == {"weekday", "start_time", "end_time"}
    for name in TOOL_NAMES:
        assert schemas[name]["additionalProperties"] is False
        assert registry.specs[name].write is False


@pytest.mark.parametrize("model", [CourseScheduleArgs, FreeTimeArgs])
@pytest.mark.parametrize("student_id", ["", "   ", 20260001, None])
def test_student_id_must_be_nonempty_string(model, student_id):
    with pytest.raises(ValidationError):
        model(student_id=student_id)


def test_student_id_preserves_leading_zero_and_rejects_context_injection():
    assert CourseScheduleArgs(student_id=" 0020260001 ").student_id == "0020260001"
    with pytest.raises(ValidationError):
        CourseScheduleArgs(student_id="0020260001", user_id="victim")


@pytest.mark.parametrize("overrides", [
    {"weekday": 0}, {"weekday": 8}, {"weekday": True}, {"weekday": "1"}, {"weekday": 1.5},
    {"start_time": "8:00"}, {"start_time": "08:60"}, {"end_time": "24:00"},
    {"start_time": "2026-10-26T08:00:00+08:00"},
    {"end_time": "08:00"}, {"end_time": "07:59"},
    {"start_time": "23:00", "end_time": "01:00"},
])
def test_classroom_rejects_invalid_weekday_or_time(overrides):
    args = {"weekday": 1, "start_time": "08:00", "end_time": "10:00", **overrides}
    with pytest.raises(ValidationError):
        EmptyClassroomArgs(**args)


@pytest.mark.parametrize("weekday", [1, 7])
def test_classroom_weekday_boundaries(weekday):
    assert EmptyClassroomArgs(weekday=weekday, start_time="00:00", end_time="23:59").weekday == weekday


def test_invalid_arguments_stop_before_c_handler():
    registry = ToolRegistry()
    stub = bind_to_registry(registry)
    args, error = Executor(registry).prepare(call(
        "get_empty_classroom", weekday=1, start_time="12:00", end_time="10:00",
    )["tool_calls"][0])
    assert args is None and error.error_code == "INVALID_ARGUMENTS"
    assert stub.calls == []


def test_raw_business_data_needs_tool_result_envelope():
    registry = ToolRegistry()
    registry.bind("get_course_schedule", lambda args, context: {"weekday": 1, "course_list": []}, simulated=True)
    with pytest.raises(ValidationError):
        registry.invoke("get_course_schedule", {"student_id": "0020260001"},
                        Context(user_id="u1", operation_id="op-1"))


def test_agent_calls_c_tools_with_results_and_no_write_confirmation():
    registry = MockCampus(now=NOW).registry()
    stub = bind_to_registry(registry)

    class Model:
        def complete(self, messages, tools, *, system, state):
            assert set(TOOL_NAMES).issubset({tool["function"]["name"] for tool in tools})
            trace = state["trace"]
            if not trace:
                return call("get_course_schedule", student_id="0020260001")
            result = json.loads(messages[-1]["content"])
            assert result["success"] and result["simulated"]
            if len(trace) == 1:
                assert result["data"]["weekday"] == 1
                return call("analyze_free_time", student_id="0020260001")
            if len(trace) == 2:
                period = result["data"]["free_periods"][0]
                start, end = period["time_range"].split("-")
                return call("get_empty_classroom", weekday=period["weekday"], start_time=start, end_time=end)
            assert result["data"]["classroom_list"][0] == {"building": "教3", "room": "402"}
            return say("模拟推荐：周二14:00–18:00，教3的402或405教室。")

    service = AgentService(Model(), registry, clock=lambda: NOW)
    response = service.chat(user_id="u1", message="我的学号是0020260001，查课表和空闲时段，推荐空教室")
    assert response.status == "completed"
    assert response.pending_confirmation is None
    assert [trace.tool_name for trace in response.trace] == list(TOOL_NAMES)
    assert all(trace.result.success and trace.result.simulated for trace in response.trace)
    assert response.trace[-1].arguments == {"weekday": 2, "start_time": "14:00", "end_time": "18:00"}
    assert [entry[0] for entry in stub.calls] == list(TOOL_NAMES)
    assert all(entry[2].user_id == "u1" for entry in stub.calls)
