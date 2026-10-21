"""宿舍报修服务与工具测试：字段校验、工单创建、归属、查询、状态流转。

对应分工文档「报修信息补全、工单创建、工单状态查询」与
「供演示使用的状态更新接口」。
"""
from __future__ import annotations

from datetime import datetime

import pytest

from backend.agent.contracts import Context, RepairArgs, TicketArgs
from backend.database.b_repository import InMemoryRepository
from backend.services.dormitory import (
    DormitoryError,
    DormitoryService,
    validate_location,
    validate_problem,
)
from backend.tools.dormitory_tools import DormitoryTools

NOW = datetime.fromisoformat("2026-10-21T10:00:00+08:00")


@pytest.fixture
def service():
    return DormitoryService(InMemoryRepository(), clock=lambda: NOW)


@pytest.fixture
def tools(service):
    return DormitoryTools(service)


def ctx(operation_id="op-1", user_id="u1"):
    return Context(user_id=user_id, operation_id=operation_id)


def create(service, *, user="u1", op="op-1", location="3号楼502室", problem="空调不制冷"):
    return service.create_ticket(user_id=user, operation_id=op,
                                 location=location, problem=problem)


# ------------------------------------------------------------------ 字段校验


@pytest.mark.parametrize("location", ["3号楼502室", "北区7号公寓305", "A栋宿舍1201", "图书馆四楼"])
def test_valid_locations_pass(location):
    assert validate_location(location)


@pytest.mark.parametrize("location", ["", "坏", "那个地方"])
def test_unlocatable_location_is_rejected(location):
    with pytest.raises(DormitoryError) as excinfo:
        validate_location(location)
    assert excinfo.value.code in {"MISSING_FIELDS", "INVALID_ARGUMENTS"}


@pytest.mark.parametrize("problem", ["空调不制冷", "水龙头漏水", "灯管闪烁", "宿舍门锁损坏"])
def test_valid_problems_pass(problem):
    assert validate_problem(problem)


@pytest.mark.parametrize("problem", ["", "坏了", "不行了", "有问题", "故障"])
def test_vague_problem_is_rejected(problem):
    with pytest.raises(DormitoryError) as excinfo:
        validate_problem(problem)
    assert excinfo.value.code in {"MISSING_FIELDS", "INVALID_ARGUMENTS"}


def test_problem_message_suggests_concrete_examples():
    with pytest.raises(DormitoryError) as excinfo:
        validate_problem("坏了")
    # 提示里应给出可照抄的例子，便于 Agent 追问。
    assert "不制冷" in excinfo.value.message or "漏水" in excinfo.value.message


# ------------------------------------------------------------------ 创建


def test_create_ticket_persists_and_is_queryable(service):
    data = create(service)
    assert data["ticket_id"].startswith("RP-")
    assert data["status"] == "pending"
    assert data["location"] == "3号楼502室"
    # 交付验收要求：报修记录真正写入，并能通过 ID 查询。
    fetched = service.get_status(user_id="u1", ticket_id=data["ticket_id"])
    assert fetched["problem"] == "空调不制冷"
    assert fetched["history"][0]["status"] == "pending"


def test_create_ticket_response_excludes_user_id(service):
    data = create(service)
    assert "user_id" not in data


def test_ticket_has_simulated_note(service):
    """模拟属性必须显式说明，不能声称已通知学校维修人员。"""
    data = create(service)
    assert "模拟" in data["simulated_note"]


def test_create_requires_valid_fields(service):
    with pytest.raises(DormitoryError) as excinfo:
        create(service, problem="坏了")
    assert excinfo.value.code in {"MISSING_FIELDS", "INVALID_ARGUMENTS"}
    # 校验失败不应留下任何记录。
    assert service.list_tickets(user_id="u1") == []


def test_same_operation_returns_same_ticket(service):
    first = create(service, op="same")
    second = create(service, op="same")
    assert first["ticket_id"] == second["ticket_id"]
    assert len(service.list_tickets(user_id="u1")) == 1


def test_same_operation_with_different_arguments_is_rejected(service):
    create(service, op="op-x")
    with pytest.raises(DormitoryError) as excinfo:
        create(service, op="op-x", problem="水龙头漏水")
    assert excinfo.value.code == "IDEMPOTENCY_KEY_REUSED"


def test_duplicate_open_ticket_is_not_created(service):
    """不同操作号但同位置同故障的未完结工单，不应重复创建。"""
    first = create(service, op="op-1")
    second = create(service, op="op-2")
    assert second["ticket_id"] == first["ticket_id"]
    assert second["duplicate"] is True
    assert len(service.list_tickets(user_id="u1")) == 1


def test_resolved_ticket_allows_new_one(service):
    """工单完成后同一故障可以重新报修。"""
    first = create(service, op="op-1")
    # 状态机要求逐级推进，不允许 pending 直接跳到 resolved。
    for status in ["accepted", "processing", "resolved"]:
        service.update_status(user_id="u1", ticket_id=first["ticket_id"], status=status)
    second = create(service, op="op-2")
    assert second["ticket_id"] != first["ticket_id"]


def test_cancelled_ticket_allows_new_one(service):
    """已取消的工单同样不再阻断重新报修。"""
    first = create(service, op="op-1")
    service.update_status(user_id="u1", ticket_id=first["ticket_id"], status="cancelled")
    second = create(service, op="op-2")
    assert second["ticket_id"] != first["ticket_id"]


def test_different_problem_creates_separate_ticket(service):
    one = create(service, op="op-1")
    two = create(service, op="op-2", problem="水龙头漏水")
    assert one["ticket_id"] != two["ticket_id"]


def test_same_problem_different_user_is_separate(service):
    one = create(service, op="op-1", user="u1")
    two = create(service, op="op-1", user="u2")
    assert one["ticket_id"] != two["ticket_id"]


# ------------------------------------------------------------------ 查询与归属


def test_ownership_is_enforced(service):
    data = create(service, user="u1")
    with pytest.raises(DormitoryError) as excinfo:
        service.get_status(user_id="attacker", ticket_id=data["ticket_id"])
    assert excinfo.value.code == "NOT_FOUND"


def test_unknown_ticket_returns_not_found(service):
    with pytest.raises(DormitoryError) as excinfo:
        service.get_status(user_id="u1", ticket_id="RP-NOTEXIST")
    assert excinfo.value.code == "NOT_FOUND"


def test_list_tickets_scoped_to_user(service):
    create(service, user="u1", op="a")
    create(service, user="u2", op="b")
    assert len(service.list_tickets(user_id="u1")) == 1


# ------------------------------------------------------------------ 状态流转


def test_status_advances_along_allowed_path(service):
    data = create(service)
    tid = data["ticket_id"]
    for status in ["accepted", "processing", "resolved"]:
        updated = service.update_status(user_id="u1", ticket_id=tid, status=status)
        assert updated["status"] == status
    assert len(updated["history"]) == 4  # 初始 + 三次流转


def test_skipping_steps_is_rejected(service):
    data = create(service)
    with pytest.raises(DormitoryError) as excinfo:
        service.update_status(user_id="u1", ticket_id=data["ticket_id"], status="resolved")
    assert excinfo.value.code == "INVALID_STATUS_TRANSITION"


def test_resolved_is_terminal(service):
    data = create(service)
    service.update_status(user_id="u1", ticket_id=data["ticket_id"], status="accepted")
    service.update_status(user_id="u1", ticket_id=data["ticket_id"], status="processing")
    service.update_status(user_id="u1", ticket_id=data["ticket_id"], status="resolved")
    with pytest.raises(DormitoryError) as excinfo:
        service.update_status(user_id="u1", ticket_id=data["ticket_id"], status="accepted")
    assert excinfo.value.code == "INVALID_STATUS_TRANSITION"


def test_unknown_status_is_rejected(service):
    data = create(service)
    with pytest.raises(DormitoryError) as excinfo:
        service.update_status(user_id="u1", ticket_id=data["ticket_id"], status="finished")
    assert excinfo.value.code == "INVALID_ARGUMENTS"


def test_non_owner_cannot_update_status(service):
    data = create(service, user="u1")
    with pytest.raises(DormitoryError) as excinfo:
        service.update_status(user_id="attacker", ticket_id=data["ticket_id"], status="accepted")
    assert excinfo.value.code == "NOT_FOUND"


def test_demo_staff_can_advance_foreign_ticket(service):
    """演示脚本模拟维修人员推进状态。"""
    data = create(service, user="u1")
    updated = service.update_status(user_id="staff", ticket_id=data["ticket_id"],
                                    status="accepted", allow_foreign=True)
    assert updated["status"] == "accepted"


# ------------------------------------------------------------------ 工具层


def test_create_tool_success_envelope(tools):
    result = tools.create_repair_ticket(
        RepairArgs(location="3号楼502室", problem="空调不制冷"), ctx()
    )
    assert result.success is True
    assert result.error_code is None
    assert result.data["ticket_id"].startswith("RP-")
    assert result.simulated is True


def test_create_tool_validation_failure_has_code(tools):
    result = tools.create_repair_ticket(
        RepairArgs(location="3号楼502室", problem="坏了"), ctx()
    )
    assert result.success is False
    assert result.error_code in {"MISSING_FIELDS", "INVALID_ARGUMENTS"}


def test_status_tool_success_envelope(tools):
    created = tools.create_repair_ticket(
        RepairArgs(location="3号楼502室", problem="空调不制冷"), ctx()
    )
    result = tools.get_repair_status(TicketArgs(ticket_id=created.data["ticket_id"]), ctx())
    assert result.success is True
    assert result.data["status"] == "pending"


def test_status_tool_rejects_foreign_ticket(tools):
    created = tools.create_repair_ticket(
        RepairArgs(location="3号楼502室", problem="空调不制冷"), ctx(user_id="u1")
    )
    result = tools.get_repair_status(
        TicketArgs(ticket_id=created.data["ticket_id"]), ctx(user_id="u2")
    )
    assert result.success is False
    assert result.error_code == "NOT_FOUND"


def test_tool_message_includes_status_label(tools):
    created = tools.create_repair_ticket(
        RepairArgs(location="3号楼502室", problem="空调不制冷"), ctx()
    )
    # message 直接展示给用户，应是中文状态而非原始英文码。
    assert "待受理" in created.message
