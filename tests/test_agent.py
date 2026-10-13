import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import pytest
from pydantic import ValidationError

from backend.agent.contracts import Context, IntervalArgs, ToolResult, fail, ok
from backend.agent.executor import Executor
from backend.agent.mock_tools import MockCampus
from backend.agent.planner import call, say
from backend.agent.service import AgentService
from backend.agent.tool_registry import SPECS

NOW = datetime.fromisoformat("2026-10-13T09:00:00+08:00")


class ScriptedModel:
    def __init__(self, *steps):
        self.steps = iter(steps)
        self.requests = []

    def complete(self, messages, tools, *, system, state):
        self.requests.append(messages)
        # 每个模型请求必须在前序全部工具结果返回之后。
        unresolved = set()
        for m in messages:
            if m.get("tool_calls"):
                assert not unresolved
                unresolved = {c["id"] for c in m["tool_calls"]}
            elif m["role"] == "tool":
                assert m["tool_call_id"] in unresolved
                unresolved.remove(m["tool_call_id"])
            elif m["role"] in {"user", "assistant"}:
                assert not unresolved
        assert not unresolved
        step = next(self.steps)
        if isinstance(step, Exception):
            raise step
        return step


@pytest.fixture
def campus():
    return MockCampus(now=NOW)


@pytest.fixture
def demo():
    return AgentService.demo(clock=lambda: NOW)


def accept(service, response, *, approved=True, user="u1"):
    return service.confirm(user_id=user, session_id=response.session_id,
        confirmation_id=response.pending_confirmation.confirmation_id, approved=approved)


def test_study_multitool_pauses_then_writes_once(demo):
    response = demo.chat(user_id="u1", message="明天下午找一个座位学习两个小时并预约")
    assert response.status == "awaiting_confirmation"
    assert [t.tool_name for t in response.trace] == ["get_schedule", "find_free_slots", "find_available_seats"]
    assert not demo.mock_campus.reservations
    assert response.pending_confirmation.arguments["start_time"] == "2026-10-14T14:00:00+08:00"
    completed = accept(demo, response)
    assert completed.status == "completed"
    assert len(demo.mock_campus.reservations) == 1
    assert "模拟" in completed.answer
    with pytest.raises(ValueError, match="没有待确认"):
        accept(demo, response)
    assert len(demo.mock_campus.reservations) == 1


def test_affair_retrieval_citation_and_multiturn_draft(demo):
    r = demo.chat(user_id="u1", message="学生证丢了，怎么办？")
    assert r.status == "completed"
    assert "MOCK-POLICY-001" in r.answer
    assert [t.tool_name for t in r.trace] == ["search_campus_knowledge", "get_affair_requirements"]
    r = demo.chat(user_id="u1", session_id=r.session_id, message="生成草稿，姓名：张三，学号：20260001")
    assert r.status == "awaiting_confirmation"
    assert r.pending_confirmation.arguments["fields"]["name"] == "张三"
    assert not demo.mock_campus.drafts
    assert accept(demo, r).status == "completed"
    assert len(demo.mock_campus.drafts) == 1


def test_dorm_clarification_preserves_problem(demo):
    r = demo.chat(user_id="u1", message="我们宿舍空调不制冷，帮我报修")
    assert "位置" in r.answer
    assert not r.trace
    r = demo.chat(user_id="u1", session_id=r.session_id, message="3号楼502室")
    assert r.pending_confirmation.arguments == {"location": "3号楼502室", "problem": "空调不制冷"}
    r = accept(demo, r)
    tid = r.trace[-1].result.data["ticket_id"]
    r = demo.chat(user_id="u1", session_id=r.session_id, message="查询工单状态 " + tid)
    assert r.trace[-1].result.data["status"] == "pending"


def test_activity_filters_each_day_before_registration(demo):
    r = demo.chat(user_id="u1", message="下周有什么不与课表冲突的计算机讲座，帮我报名")
    assert r.pending_confirmation.arguments["activity_id"] == "ACT-FREE"
    assert [t.tool_name for t in r.trace] == ["search_activities", "get_schedule", "get_schedule"]
    assert accept(demo, r).status == "completed"


def test_deny_does_not_call_write_and_next_turn_works(demo):
    r = demo.chat(user_id="u1", message="3号楼502室空调不制冷帮我报修")
    cancelled = accept(demo, r, approved=False)
    assert cancelled.status == "cancelled"
    assert cancelled.trace[-1].attempts == 0
    assert not demo.mock_campus.repairs
    next_turn = demo.chat(user_id="u1", session_id=r.session_id, message="今天有什么课")
    assert next_turn.status == "completed"


def test_owner_check_and_wrong_confirmation_id(demo):
    r = demo.chat(user_id="u1", message="3号楼502室空调不制冷帮我报修")
    for action in [
        lambda: accept(demo, r, user="attacker"),
        lambda: demo.inspect(user_id="attacker", session_id=r.session_id),
        lambda: demo.history(user_id="attacker", session_id=r.session_id),
        lambda: demo.chat(user_id="attacker", session_id=r.session_id, message="今天课表"),
        lambda: demo.confirm(user_id="u1", session_id=r.session_id, confirmation_id="wrong", approved=True),
        lambda: demo.confirm(user_id="u1", session_id=r.session_id, confirmation_id=r.pending_confirmation.confirmation_id, approved="false"),
    ]:
        with pytest.raises(ValueError):
            action()
    assert not demo.mock_campus.repairs
    assert accept(demo, r).status == "completed"


def test_pending_blocks_new_requests(demo):
    r = demo.chat(user_id="u1", message="3号楼502室空调不制冷帮我报修")
    with pytest.raises(ValueError, match="先确认"):
        demo.chat(user_id="u1", session_id=r.session_id, message="改成4号楼")
    assert not demo.mock_campus.repairs


def test_confirmation_expires():
    time = [NOW]
    service = AgentService.demo(clock=lambda: time[0], confirmation_ttl=60)
    r = service.chat(user_id="u1", message="3号楼502室空调不制冷帮我报修")
    time[0] += timedelta(seconds=60)
    r = accept(service, r)
    assert r.status == "cancelled"
    assert "超时" in r.answer
    assert not service.mock_campus.repairs
    assert r.trace[-1].result.error_code == "CONFIRMATION_EXPIRED"
    refreshed = service.inspect(user_id="u1", session_id=r.session_id)
    assert refreshed.answer == r.answer


def test_two_concurrent_confirmations_only_execute_once(demo):
    r = demo.chat(user_id="u1", message="3号楼502室空调不制冷帮我报修")
    def run():
        try:
            return accept(demo, r).status
        except ValueError:
            return "rejected"
    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(lambda _: run(), [0, 1]))
    assert sorted(statuses) == ["completed", "rejected"]
    assert len(demo.mock_campus.repairs) == 1


@pytest.mark.parametrize("name,args,code", [
    ("unknown", {}, "UNKNOWN_TOOL"),
    ("get_schedule", {"date": "明天"}, "INVALID_ARGUMENTS"),
    ("get_schedule", {"date": "2026-10-14", "user_id": "victim"}, "INVALID_ARGUMENTS"),
    ("create_repair_ticket", {"location": "3号楼"}, "INVALID_ARGUMENTS"),
    ("get_schedule", "not-json", "INVALID_ARGUMENTS"),
])
def test_model_invalid_call_never_executes(campus, name, args, code):
    message = call(name, **args) if isinstance(args, dict) else call(name)
    if not isinstance(args, dict):
        message["tool_calls"][0]["function"]["arguments"] = args
    service = AgentService(ScriptedModel(message), campus.registry(), clock=lambda: NOW)
    r = service.chat(user_id="u1", message="test")
    assert r.status == "failed" and r.trace[-1].result.error_code == code
    assert not r.pending_confirmation
    assert not campus.repairs


def test_read_retry_is_bounded(campus):
    registry = campus.registry()
    counts = []
    def unavailable(args, context):
        counts.append(context.user_id)
        return fail("TEMPORARY", "暂时不可用", retryable=True)
    registry.bind("get_schedule", unavailable, simulated=True)
    service = AgentService(ScriptedModel(call("get_schedule", date="2026-10-14")), registry, clock=lambda: NOW)
    r = service.chat(user_id="u1", message="今天课表")
    assert r.status == "failed"
    assert len(counts) == 2 and r.trace[-1].attempts == 2
    assert "暂时不可用" in r.answer


def test_read_retry_recovers(campus):
    registry = campus.registry()
    attempts = []
    def flaky(args, context):
        attempts.append(1)
        if len(attempts) == 1:
            raise TimeoutError()
        return ok({"courses": []}, "无课程")
    registry.bind("get_schedule", flaky, simulated=True)
    service = AgentService(ScriptedModel(call("get_schedule", date="2026-10-14"), say("模拟无课")), registry, clock=lambda: NOW)
    r = service.chat(user_id="u1", message="test")
    assert r.status == "completed" and r.trace[0].attempts == 2


def test_write_unknown_outcome_never_retries(campus):
    registry = campus.registry()
    attempts = []
    def write_then_timeout(args, context):
        attempts.append(context.operation_id)
        campus.create_repair_ticket(args, context)
        raise TimeoutError()
    registry.bind("create_repair_ticket", write_then_timeout, simulated=True)
    service = AgentService(ScriptedModel(call("create_repair_ticket", location="3号楼502室", problem="空调不制冷")), registry, clock=lambda: NOW)
    r = accept(service, service.chat(user_id="u1", message="test"))
    assert r.status == "failed"
    assert r.trace[-1].result.error_code == "UNKNOWN_OUTCOME"
    assert r.trace[-1].operation_id in r.answer
    assert len(attempts) == len(campus.repairs) == 1


def test_executor_itself_blocks_unapproved_write(campus):
    ex = Executor(campus.registry())
    trace = ex.run({"name": "create_repair_ticket", "id": "c1", "operation_id": "op1",
                    "arguments": {"location": "3号楼502室", "problem": "空调不制冷"}}, user_id="u1")
    assert trace.result.error_code == "CONFIRMATION_REQUIRED" and trace.attempts == 0


def test_multiple_model_calls_all_results_before_next_plan(campus):
    first = call("get_schedule", date="2026-10-14")
    first["tool_calls"] += call("find_free_slots", date="2026-10-14", duration_minutes=120)["tool_calls"]
    model = ScriptedModel(first, say("模拟查询完成"))
    service = AgentService(model, campus.registry(), clock=lambda: NOW)
    r = service.chat(user_id="u1", message="test")
    assert r.status == "completed" and len(r.trace) == 2


def test_two_writes_need_two_separate_confirmations(campus):
    first = call("create_repair_ticket", location="3号楼502室", problem="空调不制冷")
    first["tool_calls"] += call("create_repair_ticket", location="4号楼301室", problem="水龙头漏水")["tool_calls"]
    service = AgentService(ScriptedModel(first, say("模拟工单已创建")), campus.registry(), clock=lambda: NOW)
    r = service.chat(user_id="u1", message="test")
    first_id = r.pending_confirmation.confirmation_id
    r = accept(service, r)
    assert len(campus.repairs) == 1
    assert r.pending_confirmation.confirmation_id != first_id
    r = accept(service, r)
    assert len(campus.repairs) == 2 and r.status == "completed"


def test_cancel_batch_closes_remaining_calls(campus):
    first = call("create_repair_ticket", location="3号楼502室", problem="空调不制冷")
    first["tool_calls"] += call("get_schedule", date="2026-10-14")["tool_calls"]
    model = ScriptedModel(first, say("新一轮正常"))
    service = AgentService(model, campus.registry(), clock=lambda: NOW)
    r = accept(service, service.chat(user_id="u1", message="test"), approved=False)
    r = service.chat(user_id="u1", session_id=r.session_id, message="new request")
    assert r.status == "completed" and not campus.repairs


def test_step_limit_stops_infinite_planning(campus):
    service = AgentService(ScriptedModel(*[call("get_schedule", date="2026-10-14") for _ in range(5)]),
                           campus.registry(), clock=lambda: NOW, max_rounds=2)
    r = service.chat(user_id="u1", message="test")
    assert r.status == "failed" and len(r.trace) == 2 and "上限" in r.answer


@pytest.mark.parametrize("bad_reply", [say(""), {"role": "assistant", "content": 123},
                                         {"role": "assistant", "tool_calls": [{}]}])
def test_malformed_model_reply_fails_safely(campus, bad_reply):
    service = AgentService(ScriptedModel(bad_reply), campus.registry(), clock=lambda: NOW)
    assert service.chat(user_id="u1", message="test").status == "failed"


def test_model_failure_after_write_reports_partial_success(campus):
    service = AgentService(ScriptedModel(call("create_repair_ticket", location="3号楼502室", problem="空调不制冷"),
                                        RuntimeError("secret-api-key")), campus.registry(), clock=lambda: NOW)
    r = accept(service, service.chat(user_id="u1", message="test"))
    assert r.status == "failed" and "未回滚" in r.answer and "RP-" in r.answer
    assert "secret-api-key" not in r.answer


def test_runtime_result_schema_failure(campus):
    registry = campus.registry()
    registry.bind("get_schedule", lambda a, c: {"success": True, "message": "bad", "error_code": "CONFLICT"}, simulated=True)
    service = AgentService(ScriptedModel(call("get_schedule", date="2026-10-14")), registry, clock=lambda: NOW)
    r = service.chat(user_id="u1", message="test")
    assert r.status == "failed" and r.trace[-1].result.error_code == "TOOL_ERROR"


def test_arguments_reject_naive_or_reversed_time():
    for args in [
        {"start_time": "2026-10-14T14:00:00", "end_time": "2026-10-14T16:00:00"},
        {"start_time": "2026-10-14T16:00:00+08:00", "end_time": "2026-10-14T14:00:00+08:00"},
    ]:
        with pytest.raises(ValidationError):
            IntervalArgs.model_validate(args)
    args = IntervalArgs(start_time="2026-10-14T06:00:00Z", end_time="2026-10-14T08:00:00Z")
    assert args.start_time.hour == 14


def test_registry_schemas_never_offer_identity_parameters(campus):
    schemas = campus.registry().schemas()
    assert len(schemas) == 13
    for schema in schemas:
        properties = schema["function"]["parameters"]["properties"]
        assert not ({"user_id", "operation_id"} & set(properties))
    assert sum(spec.write for spec in SPECS) == 6


def test_mock_idempotency_same_operation_id(campus):
    registry = campus.registry()
    context = Context(user_id="u1", operation_id="same-request")
    args = {"location": "3号楼502室", "problem": "空调不制冷"}
    first = registry.invoke("create_repair_ticket", args, context)
    second = registry.invoke("create_repair_ticket", args, context)
    assert first.data == second.data and len(campus.repairs) == 1


def test_booking_race_rechecks_after_approval(demo):
    a = demo.chat(user_id="u1", message="明天下午学习两个小时并预约座位")
    b = demo.chat(user_id="u2", message="明天下午学习两个小时并预约座位")
    assert accept(demo, a).status == "completed"
    b = accept(demo, b, user="u2")
    assert b.status == "failed" and b.trace[-1].result.error_code == "RESERVATION_CONFLICT"
    assert len(demo.mock_campus.reservations) == 1


def test_real_binding_failure_keeps_correct_simulation_flag(campus):
    registry = campus.registry()
    def broken(args, context):
        raise TimeoutError()
    registry.bind("create_repair_ticket", broken, simulated=False)
    service = AgentService(ScriptedModel(call("create_repair_ticket", location="3号楼502室", problem="空调不制冷")), registry, clock=lambda: NOW)
    r = service.chat(user_id="u1", message="test")
    assert r.pending_confirmation.simulated is False
    r = accept(service, r)
    assert not r.trace[-1].result.simulated and "含模拟数据" not in r.answer


def test_final_answer_allowed_after_exact_tool_limit(campus):
    service = AgentService(ScriptedModel(call("get_schedule", date="2026-10-14"), say("模拟查询完成")),
        campus.registry(), clock=lambda: NOW, max_tools=1)
    r = service.chat(user_id="u1", message="test")
    assert r.status == "completed" and len(r.trace) == 1


@pytest.mark.parametrize("option", ["max_rounds", "max_tools", "read_attempts", "confirmation_ttl"])
def test_invalid_execution_limits_rejected(campus, option):
    with pytest.raises(ValueError):
        AgentService(ScriptedModel(), campus.registry(), clock=lambda: NOW, **{option: 0})
