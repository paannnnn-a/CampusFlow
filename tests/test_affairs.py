"""校园事务服务与工具测试：要求查询、草稿创建、必填校验、幂等、归属。

覆盖分工文档要求的「每个工具提供成功及失败的最小测试用例」。
"""
from __future__ import annotations

import json
from datetime import datetime

import pytest

from backend.agent.contracts import AffairArgs, Context, DraftArgs
from backend.database.b_repository import InMemoryRepository
from backend.services.affairs import (
    AffairError,
    AffairService,
    AffairSpec,
    load_affair_specs,
)
from backend.tools.affair_tools import AffairTools

NOW = datetime.fromisoformat("2026-10-21T10:00:00+08:00")


@pytest.fixture
def specs():
    return [
        AffairSpec(
            affair_type="学生证补办",
            aliases=["学生证", "补办学生证"],
            required_fields=["name", "student_id", "reason"],
            materials=["身份证原件与复印件", "一寸证件照两张"],
            steps=["到学院办公室提交材料", "等待教务处制证"],
            source="模拟整理，依据常见高校办事流程",
            department="学生事务中心",
        ),
        AffairSpec(
            affair_type="成绩单申请",
            aliases=["成绩单", "成绩证明"],
            required_fields=["name", "student_id"],
            materials=["学生证"],
            steps=["在教务系统提交申请", "三个工作日后领取"],
            source="模拟整理",
        ),
    ]


@pytest.fixture
def service(specs):
    return AffairService(InMemoryRepository(), specs=specs, clock=lambda: NOW)


@pytest.fixture
def tools(service):
    return AffairTools(service)


def ctx(operation_id="op-1", user_id="u1"):
    return Context(user_id=user_id, operation_id=operation_id)


# ------------------------------------------------------------------ 要求查询


def test_get_requirements_returns_full_structure(service):
    data = service.get_requirements("学生证补办")
    assert data["affair_type"] == "学生证补办"
    assert data["required_fields"] == ["name", "student_id", "reason"]
    assert data["materials"]
    assert data["steps"]
    # 来源必须随结果返回，答复才能标明依据。
    assert data["source"]
    assert data["source_type"] == "simulated"
    # 中文标签帮助模型与前端展示。
    assert data["required_field_labels"]["student_id"] == "学号"


@pytest.mark.parametrize("alias", ["学生证", "补办学生证", "学生证丢了", "我的学生证不见了"])
def test_alias_resolution_covers_natural_phrasings(service, alias):
    assert service.get_requirements(alias)["affair_type"] == "学生证补办"


def test_unknown_affair_reports_supported_list(service):
    with pytest.raises(AffairError) as excinfo:
        service.get_requirements("转专业")
    assert excinfo.value.code == "UNSUPPORTED_AFFAIR"
    assert "学生证补办" in excinfo.value.message


def test_blank_affair_type_is_invalid(service):
    with pytest.raises(AffairError) as excinfo:
        service.get_requirements("   ")
    assert excinfo.value.code == "INVALID_ARGUMENTS"


def test_requirements_tool_returns_success_envelope(tools):
    result = tools.get_affair_requirements(AffairArgs(affair_type="学生证补办"), ctx())
    assert result.success is True
    assert result.error_code is None
    assert result.data["affair_type"] == "学生证补办"
    # 失败不得带错误码；成功不得带错误码或 retryable。
    assert result.retryable is False


def test_requirements_tool_failure_has_error_code(tools):
    result = tools.get_affair_requirements(AffairArgs(affair_type="不存在的业务"), ctx())
    assert result.success is False
    assert result.error_code == "UNSUPPORTED_AFFAIR"
    assert result.data == {}


# ------------------------------------------------------------------ 草稿创建


def test_create_draft_persists_and_is_queryable(service):
    data = service.create_draft(
        user_id="u1", operation_id="op-1", affair_type="学生证补办",
        fields={"name": "张三", "student_id": "20260001", "reason": "学生证遗失"},
    )
    assert data["draft_id"].startswith("DFT-")
    assert data["status"] == "draft"
    # 交付验收要求：草稿真正写入，并能通过 ID 查询。
    fetched = service.get_draft(user_id="u1", draft_id=data["draft_id"])
    assert fetched["fields"]["student_id"] == "20260001"
    assert fetched["affair_type"] == "学生证补办"


def test_create_draft_requires_all_fields(service):
    with pytest.raises(AffairError) as excinfo:
        service.create_draft(
            user_id="u1", operation_id="op-1", affair_type="学生证补办",
            fields={"name": "张三"},
        )
    assert excinfo.value.code == "MISSING_FIELDS"
    # 必须指出缺哪些字段，模型才能精准追问。
    assert "学号" in excinfo.value.message
    assert "申请说明" in excinfo.value.message


def test_blank_field_counts_as_missing(service):
    with pytest.raises(AffairError) as excinfo:
        service.create_draft(
            user_id="u1", operation_id="op-1", affair_type="学生证补办",
            fields={"name": "张三", "student_id": "  ", "reason": "遗失"},
        )
    assert excinfo.value.code == "MISSING_FIELDS"
    assert "学号" in excinfo.value.message


def test_draft_is_idempotent_for_same_operation(service):
    """相同操作号重复提交必须返回同一草稿，不能产生第二条记录。"""
    args = dict(user_id="u1", operation_id="same-op", affair_type="学生证补办",
                fields={"name": "张三", "student_id": "20260001", "reason": "遗失"})
    first = service.create_draft(**args)
    second = service.create_draft(**args)
    assert first["draft_id"] == second["draft_id"]
    assert len(service.list_drafts(user_id="u1")) == 1


def test_same_operation_with_different_fields_is_rejected(service):
    service.create_draft(user_id="u1", operation_id="op-x", affair_type="学生证补办",
                         fields={"name": "张三", "student_id": "1", "reason": "遗失"})
    with pytest.raises(AffairError) as excinfo:
        service.create_draft(user_id="u1", operation_id="op-x", affair_type="学生证补办",
                             fields={"name": "李四", "student_id": "2", "reason": "遗失"})
    assert excinfo.value.code == "IDEMPOTENCY_KEY_REUSED"


def test_different_operations_create_separate_drafts(service):
    """业务上允许重复提交申请，因此不同操作号应产生独立草稿。"""
    one = service.create_draft(user_id="u1", operation_id="op-1", affair_type="学生证补办",
                               fields={"name": "张三", "student_id": "1", "reason": "遗失"})
    two = service.create_draft(user_id="u1", operation_id="op-2", affair_type="学生证补办",
                               fields={"name": "张三", "student_id": "1", "reason": "遗失"})
    assert one["draft_id"] != two["draft_id"]


def test_draft_ownership_is_enforced(service):
    data = service.create_draft(user_id="u1", operation_id="op-1", affair_type="学生证补办",
                                fields={"name": "张三", "student_id": "1", "reason": "遗失"})
    with pytest.raises(AffairError) as excinfo:
        service.get_draft(user_id="attacker", draft_id=data["draft_id"])
    assert excinfo.value.code == "NOT_FOUND"


def test_unknown_draft_returns_not_found(service):
    with pytest.raises(AffairError) as excinfo:
        service.get_draft(user_id="u1", draft_id="DFT-NOTEXIST")
    assert excinfo.value.code == "NOT_FOUND"


def test_extra_fields_are_reported_not_silently_dropped(service):
    data = service.create_draft(
        user_id="u1", operation_id="op-1", affair_type="学生证补办",
        fields={"name": "张三", "student_id": "1", "reason": "遗失", "favorite_color": "蓝"},
    )
    assert data["ignored_fields"] == ["favorite_color"]


def test_draft_response_excludes_user_id(service):
    data = service.create_draft(user_id="u1", operation_id="op-1", affair_type="学生证补办",
                                fields={"name": "张三", "student_id": "1", "reason": "遗失"})
    assert "user_id" not in data
    assert "user_id" not in json.dumps(data)


def test_list_drafts_is_scoped_to_user(service):
    service.create_draft(user_id="u1", operation_id="a", affair_type="学生证补办",
                         fields={"name": "张三", "student_id": "1", "reason": "遗失"})
    service.create_draft(user_id="u2", operation_id="b", affair_type="学生证补办",
                         fields={"name": "李四", "student_id": "2", "reason": "遗失"})
    assert len(service.list_drafts(user_id="u1")) == 1


# ------------------------------------------------------------------ 工具层


def test_draft_tool_success_envelope(tools):
    result = tools.create_affair_draft(
        DraftArgs(affair_type="学生证补办",
                  fields={"name": "张三", "student_id": "20260001", "reason": "遗失"}),
        ctx(),
    )
    assert result.success is True
    assert result.error_code is None
    assert result.data["draft_id"].startswith("DFT-")
    assert result.simulated is True


def test_draft_tool_missing_fields_returns_error_code(tools):
    result = tools.create_affair_draft(
        DraftArgs(affair_type="学生证补办", fields={"name": "张三"}), ctx()
    )
    assert result.success is False
    assert result.error_code == "MISSING_FIELDS"


def test_draft_tool_uses_context_identity_not_model_args(tools):
    """模型无法指定 user_id：同一 operation_id 在另一个用户下应是新草稿。"""
    args = DraftArgs(affair_type="学生证补办",
                     fields={"name": "张三", "student_id": "1", "reason": "遗失"})
    first = tools.create_affair_draft(args, ctx(operation_id="shared"))
    second = tools.create_affair_draft(args, ctx(operation_id="shared", user_id="u2"))
    assert first.success and second.success
    assert first.data["draft_id"] != second.data["draft_id"]


def test_service_requires_user_id(specs):
    service = AffairService(InMemoryRepository(), specs=specs)
    with pytest.raises(AffairError) as excinfo:
        service.create_draft(user_id="", operation_id="op", affair_type="学生证补办",
                             fields={"name": "张三", "student_id": "1", "reason": "遗失"})
    assert excinfo.value.code == "UNAUTHORIZED"


# ------------------------------------------------------------------ 种子数据


def test_seed_loader_skips_entries_without_source(tmp_path):
    path = tmp_path / "campus_affairs.json"
    path.write_text(json.dumps({"affairs": [
        {"affair_type": "有效事务", "required_fields": ["name"], "source": "某处"},
        {"affair_type": "缺来源", "required_fields": ["name"]},
        {"required_fields": ["name"], "source": "某处"},
    ]}, ensure_ascii=False), encoding="utf-8")
    specs = load_affair_specs(path)
    # 没有来源或没有类型名的条目必须被丢弃，避免编造官方流程。
    assert [spec.affair_type for spec in specs] == ["有效事务"]


def test_seed_loader_returns_empty_for_missing_file(tmp_path):
    assert load_affair_specs(tmp_path / "nope.json") == []


def test_seed_loader_rejects_broken_json(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(AffairError) as excinfo:
        load_affair_specs(path)
    assert excinfo.value.code == "SEED_INVALID"


def test_service_without_seed_reports_no_supported_affairs(tmp_path):
    service = AffairService(InMemoryRepository(), seed_path=tmp_path / "missing.json")
    with pytest.raises(AffairError) as excinfo:
        service.get_requirements("学生证补办")
    assert excinfo.value.code == "UNSUPPORTED_AFFAIR"
    assert "无" in excinfo.value.message


# ------------------------------------------------- 仓库内真实种子数据的约束
#
# 以下用例校验 data/seed/campus_affairs.json 本身的质量，
# 而不只是校验加载器。种子数据被改坏时这里会失败。

from pathlib import Path as _Path

_SEED = _Path(__file__).resolve().parents[1] / "data" / "seed" / "campus_affairs.json"
_KNOWLEDGE = _Path(__file__).resolve().parents[1] / "data" / "knowledge"


def test_repo_seed_is_present_and_loads():
    if not _SEED.exists():
        pytest.skip("种子文件尚未提供")
    specs = load_affair_specs(_SEED)
    assert specs, "种子文件存在但一条都没加载成功，通常是缺 source"
    # 每条都必须带出处，且出处必须说明是整理稿而非官方发布件。
    for spec in specs:
        assert spec.source.strip()
        assert "整理稿" in spec.source
        assert spec.source_type == "official"
        assert spec.required_fields, spec.affair_type


def test_repo_seed_affair_types_match_knowledge_docs():
    """事务名要与知识文档对应，否则检索与结构化要求会各说一套。"""
    if not _SEED.exists() or not _KNOWLEDGE.is_dir():
        pytest.skip("种子文件或知识文档尚未提供")
    from backend.rag.ingest import load_directory

    doc_text = "\n".join(chunk.content for chunk in load_directory(_KNOWLEDGE))
    for spec in load_affair_specs(_SEED):
        # 事务名去掉末尾的「申请 / 处理」后应能在文档里找到，如「学生证补办」。
        core = spec.affair_type.replace("申请", "").replace("处理", "")
        assert core in doc_text, "%s 在知识文档中没有对应内容" % spec.affair_type


@pytest.mark.parametrize("phrase", [
    "学生证丢了", "饭卡丢了", "国励", "缓考", "换专业",
    "书超期了", "借个教室办活动", "空调不制冷", "成绩单怎么开",
])
def test_repo_seed_resolves_natural_phrasings(phrase):
    """用户口语化说法必须能落到某个事务上，否则只能回「暂未收录」。"""
    if not _SEED.exists():
        pytest.skip("种子文件尚未提供")
    service = AffairService(InMemoryRepository(), seed_path=_SEED)
    assert service.resolve(phrase).affair_type


def test_repo_seed_draft_persists_and_is_queryable():
    """验收要求：草稿要真的落库并能按 ID 查回。"""
    if not _SEED.exists():
        pytest.skip("种子文件尚未提供")
    service = AffairService(InMemoryRepository(), seed_path=_SEED)
    spec = service.resolve("学生证丢了")
    fields = {key: "测试值" for key in spec.required_fields}
    draft = service.create_draft(user_id="u1", operation_id="op-1",
                                affair_type=spec.affair_type, fields=fields)
    again = service.get_draft(user_id="u1", draft_id=draft["draft_id"])
    assert again["draft_id"] == draft["draft_id"]
    # 用户填写的字段要原样落库并可查回。
    assert again["fields"] == fields
    # 草稿只记录用户输入；材料清单属于事务定义，走 get_requirements 取，
    # 不塞进草稿记录里，避免存下一份会过期的政策快照。
    assert service.get_requirements(spec.affair_type)["materials"]
