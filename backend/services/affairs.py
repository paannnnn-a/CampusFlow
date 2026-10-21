"""校园事务服务：办事流程/材料查询与模拟申请草稿。

职责边界：只处理业务规则与数据读写，不涉及 Agent 编排，也不拼 SQL。
工具层（``backend/tools/affair_tools.py``）负责把这里的结果包装成 A 的统一信封。

办理要求的来源
--------------
CampusFlow 要求「标明资料来源或模拟属性」，因此办事要求**不写死在代码里**，
而是从 ``data/seed/campus_affairs.json`` 读取。该文件由 B 依据真实通知整理，
每条记录必须带 ``source`` 字段说明出处；未标注来源的事务一律拒绝加载，
避免出现来路不明的"官方流程"。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence
from uuid import uuid4

from ..database.b_repository import (
    AffairDraftRecord,
    CampusRepository,
    DuplicateOperation,
)

DEFAULT_SEED_PATH = Path("data/seed/campus_affairs.json")

#: 草稿字段的展示名，用于生成可读的缺字段提示。
FIELD_LABELS = {
    "name": "姓名",
    "student_id": "学号",
    "reason": "申请说明",
    "contact": "联系电话",
    "department": "院系",
    "class_name": "班级",
    "lost_date": "遗失日期",
    "lost_place": "遗失地点",
    "description": "情况说明",
    "dorm_building": "宿舍楼栋",
    "room": "房间号",
    "time_range": "期望办理时间",
}

_SUPPORTED_ENTITY = ["affairs_requests"]


class AffairError(Exception):
    """业务错误，携带错误码供工具层直接返回。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class AffairSpec:
    """一种事务的办理要求。"""

    affair_type: str
    aliases: Sequence[str]
    required_fields: Sequence[str]
    materials: Sequence[str]
    steps: Sequence[str]
    source: str
    source_type: str = "simulated"
    department: str = ""

    def to_public(self) -> Dict[str, Any]:
        return {
            "affair_type": self.affair_type,
            "required_fields": list(self.required_fields),
            "required_field_labels": {
                key: FIELD_LABELS.get(key, key) for key in self.required_fields
            },
            "materials": list(self.materials),
            "steps": list(self.steps),
            "department": self.department,
            "source": self.source,
            "source_type": self.source_type,
        }


def load_affair_specs(path: Path = DEFAULT_SEED_PATH) -> List[AffairSpec]:
    """加载事务定义。文件缺失或条目缺来源时返回空列表，不猜测内容。"""
    path = Path(path)
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise AffairError("SEED_INVALID", "事务数据文件格式错误：%s" % path.name) from exc

    items = payload.get("affairs") if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        raise AffairError("SEED_INVALID", "事务数据必须是数组")

    specs: List[AffairSpec] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        affair_type = str(item.get("affair_type") or "").strip()
        source = str(item.get("source") or "").strip()
        required = item.get("required_fields") or []
        # 没有类型名或没有来源的记录直接跳过：宁可查不到，也不能编造流程。
        if not affair_type or not source or not required:
            continue
        specs.append(AffairSpec(
            affair_type=affair_type,
            aliases=[str(a) for a in (item.get("aliases") or [])],
            required_fields=[str(f) for f in required],
            materials=[str(m) for m in (item.get("materials") or [])],
            steps=[str(s) for s in (item.get("steps") or [])],
            source=source,
            source_type=str(item.get("source_type") or "simulated"),
            department=str(item.get("department") or ""),
        ))
    return specs


def _normalize(text: str) -> str:
    """统一别名匹配：去空格、标点与常见前后缀。"""
    return re.sub(r"[\s　·、，,。.．:：/－\-—_]+", "", text or "").lower()


class AffairService:
    def __init__(self, repository: CampusRepository, *,
                 specs: Optional[Sequence[AffairSpec]] = None,
                 seed_path: Path = DEFAULT_SEED_PATH,
                 clock=None):
        self.repository = repository
        self._specs = list(specs) if specs is not None else load_affair_specs(seed_path)
        self._clock = clock or (lambda: datetime.now().astimezone())

    # ------------------------------------------------------------ 查询

    @property
    def affair_types(self) -> List[str]:
        return [spec.affair_type for spec in self._specs]

    def resolve(self, affair_type: str) -> AffairSpec:
        """按名称或别名找到事务定义。"""
        target = _normalize(affair_type)
        if not target:
            raise AffairError("INVALID_ARGUMENTS", "请说明要办理的事务类型")
        for spec in self._specs:
            candidates = [spec.affair_type, *spec.aliases]
            if any(_normalize(c) == target for c in candidates):
                return spec
            # 兼容用户说「学生证丢了」「补办学生证」这类含别名的说法。
            if any(len(_normalize(c)) >= 2 and _normalize(c) in target for c in candidates):
                return spec
        raise AffairError(
            "UNSUPPORTED_AFFAIR",
            "暂未收录「%s」的办理要求，可办理的事务：%s"
            % (affair_type, "、".join(self.affair_types) or "无"),
        )

    def get_requirements(self, affair_type: str) -> Dict[str, Any]:
        return self.resolve(affair_type).to_public()

    # ------------------------------------------------------------ 草稿

    def create_draft(self, *, user_id: str, operation_id: str, affair_type: str,
                     fields: Mapping[str, str]) -> Dict[str, Any]:
        """生成模拟申请草稿。

        写入规则（对应 tool_contract）：
        1. 必填字段缺失直接拒绝，并指出缺哪些字段（不给模型猜的机会）。
        2. 幂等键相同 + 参数相同 → 返回原草稿，不重复写入。
        3. 幂等键相同 + 参数不同 → 拒绝。
        """
        if not user_id or not user_id.strip():
            raise AffairError("UNAUTHORIZED", "缺少可信用户身份")
        if not operation_id or not operation_id.strip():
            raise AffairError("INVALID_ARGUMENTS", "缺少操作号")

        spec = self.resolve(affair_type)
        normalized = self._normalize_fields(fields)
        self._require_fields(spec, normalized)

        arguments = {"affair_type": spec.affair_type, "fields": normalized}
        key = {"user_id": user_id, "tool_name": "create_affair_draft", "operation_id": operation_id}

        try:
            cached = self.repository.begin_operation(arguments=arguments, **key)
        except DuplicateOperation as exc:
            raise AffairError("IDEMPOTENCY_KEY_REUSED", str(exc)) from exc
        if cached is not None:
            return cached

        # 必填字段齐备但出现未登记字段时提示，避免模型塞入无关内容。
        unknown = [
            key_name for key_name in normalized
            if key_name not in spec.required_fields
        ]

        now = self._clock()
        record = AffairDraftRecord(
            draft_id="DFT-" + uuid4().hex[:10].upper(),
            affair_type=spec.affair_type,
            fields=normalized,
            status="draft",
            user_id=user_id,
            created_at=now,
        )
        self.repository.save_affair_draft(record)

        data = record.to_public()
        data["materials"] = list(spec.materials)
        data["steps"] = list(spec.steps)
        data["source"] = spec.source
        data["source_type"] = spec.source_type
        if unknown:
            data["ignored_fields"] = unknown
        self.repository.finish_operation(data=data, **key)
        return data

    def get_draft(self, *, user_id: str, draft_id: str) -> Dict[str, Any]:
        """按编号查询草稿，并校验归属。"""
        record = self.repository.get_affair_draft(draft_id)
        if record is None or record.user_id != user_id:
            raise AffairError("NOT_FOUND", "草稿不存在或不属于当前用户")
        return record.to_public()

    def list_drafts(self, *, user_id: str) -> List[Dict[str, Any]]:
        return [r.to_public() for r in self.repository.list_affair_drafts(user_id)]

    # ------------------------------------------------------------ 内部

    @staticmethod
    def _normalize_fields(fields: Mapping[str, str]) -> Dict[str, str]:
        if not isinstance(fields, Mapping):
            raise AffairError("INVALID_ARGUMENTS", "fields 必须是键值对象")
        normalized: Dict[str, str] = {}
        for raw_key, raw_value in fields.items():
            key = str(raw_key).strip()
            if not key:
                continue
            value = "" if raw_value is None else str(raw_value).strip()
            normalized[key] = value
        return normalized

    @staticmethod
    def _require_fields(spec: AffairSpec, fields: Mapping[str, str]) -> None:
        missing = [key for key in spec.required_fields if not fields.get(key, "").strip()]
        if missing:
            labels = "、".join(FIELD_LABELS.get(key, key) for key in missing)
            raise AffairError(
                "MISSING_FIELDS",
                "「%s」缺少必填信息：%s（字段名：%s）" % (spec.affair_type, labels, ", ".join(missing)),
            )
