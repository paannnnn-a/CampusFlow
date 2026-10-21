"""B 侧集成测试：工具真正绑进 A 的 registry 后能否被正常调用。

这层测试的意义在于验证「接口契约」而非业务细节：
- B 的 5 个工具能覆盖 Mock 的同名绑定，不影响 C 侧的 8 个工具；
- 参数 Pydantic 校验、统一信封、``simulated`` 覆盖、写操作分类都由 A 的
  registry 保证，这里验证 B 的实现不会破坏这些约束。
"""
from __future__ import annotations

from datetime import datetime

import pytest

from backend.agent.contracts import (
    AffairArgs,
    Context,
    DraftArgs,
    KnowledgeArgs,
    RepairArgs,
    TicketArgs,
    ToolResult,
)
from backend.agent.mock_tools import MockCampus
from backend.database.b_repository import InMemoryRepository
from backend.rag import HashingEmbedder, KnowledgeBase, MemoryIndex, load_document
from backend.services.affairs import AffairSpec
from backend.tools import B_TOOL_NAMES, bind_b_tools, build_bundle

NOW = datetime.fromisoformat("2026-10-21T10:00:00+08:00")


@pytest.fixture
def spec():
    return AffairSpec(
        affair_type="学生证补办",
        aliases=["学生证"],
        required_fields=["name", "student_id", "reason"],
        materials=["身份证原件与复印件"],
        steps=["到学院办公室提交材料"],
        source="模拟整理",
    )


@pytest.fixture
def knowledge_base(tmp_path):
    doc = tmp_path / "学生证补办.md"
    doc.write_text(
        "---\ntitle: 学生证补办流程\nsource_type: simulated\nsource: 模拟整理\n---\n"
        "## 所需材料\n\n- 身份证原件与复印件\n- 一寸证件照两张\n",
        encoding="utf-8",
    )
    kb = KnowledgeBase(embedder=HashingEmbedder(512), index=MemoryIndex(), min_score=0.1)
    kb.build(load_document(doc))
    return kb


@pytest.fixture
def registry_with_b(knowledge_base, spec):
    """先绑 Mock（模拟 A 当前状态），再用 B 覆盖 B 负责的 5 个工具。"""
    registry = MockCampus(now=NOW).registry()
    bundle = build_bundle(
        repository=InMemoryRepository(),
        knowledge_base=knowledge_base,
        clock=lambda: NOW,
    )
    # 用固定事务定义替换默认（默认从 seed 文件读，测试环境没有该文件）。
    from backend.services.affairs import AffairService
    from backend.tools.affair_tools import AffairTools

    bundle.affair_tools = AffairTools(
        AffairService(bundle.repository, specs=[spec], clock=lambda: NOW)
    )
    bind_b_tools(registry, bundle)
    return registry, bundle


def ctx(operation_id="op-1", user_id="u1"):
    return Context(user_id=user_id, operation_id=operation_id)


# ------------------------------------------------------------------ 绑定覆盖


def test_b_tools_are_bound_and_schemas_exposed(registry_with_b):
    registry, _ = registry_with_b
    for name in B_TOOL_NAMES:
        assert registry.ready(name), "%s 未绑定" % name
    exposed = {schema["function"]["name"] for schema in registry.schemas()}
    assert set(B_TOOL_NAMES).issubset(exposed)


def test_c_tools_still_use_mock(registry_with_b):
    """覆盖 B 的工具不应误伤 C 负责的工具。"""
    registry, _ = registry_with_b
    for name in ["get_schedule", "find_free_slots", "book_study_seat", "search_activities"]:
        assert registry.ready(name)
    result = registry.invoke("get_schedule", {"date": "2026-10-21"}, ctx())
    assert result.success


def test_write_classification_follows_specs(registry_with_b):
    """B 的适配器不得改变写操作分类——这由 A 的 SPECS 固定。"""
    registry, _ = registry_with_b
    assert registry.specs["create_affair_draft"].write is True
    assert registry.specs["create_repair_ticket"].write is True
    assert registry.specs["search_campus_knowledge"].write is False
    assert registry.specs["get_repair_status"].write is False


def test_simulated_flag_is_set_by_registry(registry_with_b):
    registry, _ = registry_with_b
    result = registry.invoke("search_campus_knowledge", {"query": "学生证"}, ctx())
    assert result.simulated is True


# ------------------------------------------------------------------ 知识工具


def test_knowledge_tool_returns_citable_sources(registry_with_b):
    registry, _ = registry_with_b
    result = registry.invoke("search_campus_knowledge", {"query": "补办学生证需要哪些材料"}, ctx())
    assert result.success
    sources = result.data["sources"]
    assert sources
    # 每条依据都必须可引用。
    for source in sources:
        assert source["source_id"]
        assert source["title"]
        assert source["content"]
    assert any("证件照" in s["content"] for s in sources)


def test_knowledge_tool_returns_empty_without_evidence(registry_with_b):
    registry, _ = registry_with_b
    result = registry.invoke("search_campus_knowledge", {"query": "量子力学实验安全规范"}, ctx())
    assert result.success
    assert result.data["sources"] == []
    # 查不到时必须明说，而不是给出无关内容。
    assert "未" in result.message


# ------------------------------------------------------------------ 事务工具


def test_affair_tools_full_flow(registry_with_b):
    registry, _ = registry_with_b
    requirements = registry.invoke("get_affair_requirements", {"affair_type": "学生证"}, ctx())
    assert requirements.success
    assert requirements.data["required_fields"] == ["name", "student_id", "reason"]

    draft = registry.invoke("create_affair_draft", {
        "affair_type": "学生证补办",
        "fields": {"name": "张三", "student_id": "20260001", "reason": "遗失"},
    }, ctx(operation_id="draft-1"))
    assert draft.success
    assert draft.data["draft_id"].startswith("DFT-")


def test_affair_draft_missing_fields_is_rejected(registry_with_b):
    registry, _ = registry_with_b
    result = registry.invoke("create_affair_draft", {
        "affair_type": "学生证补办", "fields": {"name": "张三"},
    }, ctx())
    assert result.success is False
    assert result.error_code == "MISSING_FIELDS"


def test_affair_draft_idempotent_across_calls(registry_with_b):
    registry, _ = registry_with_b
    args = {"affair_type": "学生证补办",
            "fields": {"name": "张三", "student_id": "1", "reason": "遗失"}}
    first = registry.invoke("create_affair_draft", args, ctx(operation_id="same"))
    second = registry.invoke("create_affair_draft", args, ctx(operation_id="same"))
    assert first.success and second.success
    assert first.data["draft_id"] == second.data["draft_id"]


# ------------------------------------------------------------------ 宿舍工具


def test_dormitory_tools_full_flow(registry_with_b):
    registry, _ = registry_with_b
    created = registry.invoke("create_repair_ticket",
                              {"location": "3号楼502室", "problem": "空调不制冷"},
                              ctx(operation_id="repair-1"))
    assert created.success
    ticket_id = created.data["ticket_id"]

    status = registry.invoke("get_repair_status", {"ticket_id": ticket_id}, ctx())
    assert status.success
    assert status.data["status"] == "pending"
    assert status.data["location"] == "3号楼502室"


def test_dormitory_ownership_via_registry(registry_with_b):
    registry, _ = registry_with_b
    created = registry.invoke("create_repair_ticket",
                              {"location": "3号楼502室", "problem": "空调不制冷"},
                              ctx(user_id="u1"))
    result = registry.invoke("get_repair_status",
                             {"ticket_id": created.data["ticket_id"]}, ctx(user_id="u2"))
    assert result.success is False
    assert result.error_code == "NOT_FOUND"


# ------------------------------------------------------------------ 契约不变量


def test_all_results_satisfy_envelope_invariant(registry_with_b):
    """成功不带错误码，失败必带错误码——由 ToolResult 自身保证，这里抽查。

    这里的参数必须是**合法参数**：非法参数会在 ``model_validate`` 阶段抛
    ``ValidationError``，那是 ToolRegistry 的职责边界，由 executor 捕获后
    统一转成信封（见 ``backend/agent/executor.py``），不属于本用例范围。
    """
    registry, _ = registry_with_b
    cases = [
        ("search_campus_knowledge", {"query": "学生证"}),
        ("get_affair_requirements", {"affair_type": "不存在的事务"}),
        ("create_repair_ticket", {"location": "C1 宿舍 302", "problem": "空调不制冷"}),
        ("get_repair_status", {"ticket_id": "RP-NONE"}),
    ]
    for name, args in cases:
        result = registry.invoke(name, args, ctx())
        assert isinstance(result, ToolResult)
        if result.success:
            assert result.error_code is None
            # data 必须是对象，不能是 null（tool_contract 明文要求）。
            assert isinstance(result.data, dict)
        else:
            assert result.error_code
            assert result.data == {}


def test_invalid_args_are_rejected_before_handler_runs(registry_with_b):
    """参数校验应在进入 handler 之前就拦住，而不是让业务层收到脏数据。

    这是防御性告知：``invoke`` 直接调用会抛 ValidationError，
    生产路径由 executor 兜成信封，保证 Agent 不会因一次坏参数中断。
    """
    from pydantic import ValidationError
    from backend.agent.executor import Executor

    registry, _ = registry_with_b
    with pytest.raises(ValidationError):
        registry.invoke("create_repair_ticket", {"location": "坏", "problem": "坏了"}, ctx())

    # executor 兜底后应得到带错误码的信封，而非异常冒泡。
    executor = Executor(registry) if _accepts_registry(Executor) else None
    if executor is not None:
        trace = executor.run(
            {"name": "create_repair_ticket", "arguments": {"location": "坏", "problem": "坏了"},
             "operation_id": "op-x", "id": "c1"}, user_id="u1", approved=True)
        assert not trace.result.success
        assert trace.result.error_code


def _accepts_registry(cls):
    import inspect

    return "registry" in inspect.signature(cls.__init__).parameters


def test_models_cannot_inject_user_id(registry_with_b):
    """A 的参数模型设置了 extra=forbid，模型塞 user_id 会被拒绝。"""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        RepairArgs(location="3号楼502室", problem="漏水", user_id="victim")
