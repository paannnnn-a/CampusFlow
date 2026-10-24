"""13 个 Mock 的正常返回、非法参数，以及关键业务拒绝。"""
from datetime import datetime

import pytest
from pydantic import ValidationError

from backend.agent.contracts import Context, ToolResult
from backend.agent.mock_tools import MockCampus
from backend.agent.tool_registry import SPECS

MOCK_SPECS = tuple(spec for spec in SPECS if hasattr(MockCampus, spec.name))


@pytest.fixture
def fixture():
    campus = MockCampus(now=datetime.fromisoformat("2026-10-13T09:00:00+08:00"))
    registry = campus.registry()
    counter = [0]
    def invoke(name, **args):
        counter[0] += 1
        return registry.invoke(name, args, Context(user_id="u1", operation_id=str(counter[0])))
    return campus, registry, invoke


@pytest.mark.parametrize("spec", MOCK_SPECS, ids=lambda s: s.name)
def test_every_mock_rejects_invalid_parameters(fixture, spec):
    _, registry, _ = fixture
    # 所有接口均有必填参数；未知身份字段始终被拒绝。
    with pytest.raises(ValidationError):
        registry.invoke(spec.name, {"user_id": "victim"}, Context(user_id="u1", operation_id="op"))


@pytest.mark.parametrize("name", [s.name for s in MOCK_SPECS])
def test_every_mock_has_valid_success_result(fixture, name):
    _, _, invoke = fixture
    interval = {"start_time": "2026-10-14T14:00:00+08:00", "end_time": "2026-10-14T16:00:00+08:00"}
    args = {
        "get_schedule": {"date": "2026-10-14"},
        "find_free_slots": {"date": "2026-10-14", "duration_minutes": 120},
        "find_available_seats": interval,
        "book_study_seat": {**interval, "seat_id": "LIB-A301-01"},
        "search_campus_knowledge": {"query": "学生证补办"},
        "get_affair_requirements": {"affair_type": "学生证补办"},
        "create_affair_draft": {"affair_type": "学生证补办", "fields": {"name": "张三", "student_id": "20260001", "reason": "遗失"}},
        "search_activities": {"keywords": "计算机", "start_time": "2026-10-19T00:00:00+08:00", "end_time": "2026-10-26T00:00:00+08:00"},
        "register_activity": {"activity_id": "ACT-FREE"},
        "create_repair_ticket": {"location": "3号楼502室", "problem": "空调不制冷"},
    }
    if name == "cancel_study_reservation":
        r = invoke("book_study_seat", seat_id="LIB-A301-01", **interval)
        args[name] = {"reservation_id": r.data["reservation_id"]}
    elif name == "cancel_activity_registration":
        r = invoke("register_activity", activity_id="ACT-FREE")
        args[name] = {"registration_id": r.data["registration_id"]}
    elif name == "get_repair_status":
        r = invoke("create_repair_ticket", location="3号楼502室", problem="空调不制冷")
        args[name] = {"ticket_id": r.data["ticket_id"]}
    r = invoke(name, **args[name])
    assert r.success and r.simulated and r.error_code is None


def test_business_conflicts_and_ownership(fixture):
    _, registry, invoke = fixture
    assert invoke("register_activity", activity_id="ACT-CONFLICT").error_code == "COURSE_CONFLICT"
    assert invoke("register_activity", activity_id="missing").error_code == "NOT_FOUND"
    assert invoke("register_activity", activity_id="ACT-FREE").success
    assert invoke("register_activity", activity_id="ACT-FREE").error_code == "DUPLICATE_REGISTRATION"
    assert invoke("get_affair_requirements", affair_type="未知事务").error_code == "UNSUPPORTED_AFFAIR"
    assert invoke("create_affair_draft", affair_type="学生证补办", fields={}).error_code == "MISSING_FIELDS"
    assert invoke("cancel_study_reservation", reservation_id="missing").error_code == "NOT_FOUND"
    assert invoke("cancel_activity_registration", registration_id="missing").error_code == "NOT_FOUND"
    ticket = invoke("create_repair_ticket", location="3号楼502室", problem="漏水").data["ticket_id"]
    r = registry.invoke("get_repair_status", {"ticket_id": ticket}, Context(user_id="attacker", operation_id="op"))
    assert r.error_code == "NOT_FOUND"
    assert invoke("search_campus_knowledge", query="未知政策").data["sources"] == []


def test_tool_result_rejects_string_success():
    with pytest.raises(ValidationError):
        ToolResult(success="false", data={}, message="bad", error_code="ERROR")


def test_idempotency_key_cannot_change_parameters(fixture):
    campus, registry, _ = fixture
    context = Context(user_id="u1", operation_id="same-op")
    first = registry.invoke("create_repair_ticket", {"location": "3号楼502室", "problem": "漏水"}, context)
    second = registry.invoke("create_repair_ticket", {"location": "4号楼301室", "problem": "漏水"}, context)
    assert first.success and second.error_code == "IDEMPOTENCY_KEY_REUSED"
    assert len(campus.repairs) == 1


def test_activity_search_accepts_multiple_topic_terms(fixture):
    _, _, invoke = fixture
    result = invoke("search_activities", keywords="计算机 讲座",
                    start_time="2026-10-19T00:00:00+08:00", end_time="2026-10-26T00:00:00+08:00")
    assert result.success and len(result.data["activities"]) == 2
