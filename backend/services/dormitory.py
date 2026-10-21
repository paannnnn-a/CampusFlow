"""宿舍生活服务：报修工单创建、字段校验、状态查询与演示用状态更新。

职责边界：业务规则与数据读写；不涉及 Agent 编排与 SQL 拼接。

字段校验原则
------------
``create_repair_ticket`` 在工具签名里只有 ``location`` 和 ``problem``，
但真实报修需要位置可定位、故障可派单。因此业务层做以下校验：

1. 位置必须能识别出楼栋与房间，或至少含明确方位词（宿舍/公寓/楼/室/间），
   避免「坏了」这类无法派单的输入。
2. 故障描述必须有实质性内容（去掉无意义词后长度足够），避免「坏了」「不行了」。
3. 同一用户对同一位置、同一故障的**未完结**工单不重复创建，返回已有编号并提示。

校验失败返回 ``MISSING_FIELDS`` 或 ``INVALID_ARGUMENTS``，让 Agent 能继续追问。
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import uuid4

from ..database.b_repository import (
    REPAIR_TRANSITIONS,
    CampusRepository,
    DuplicateOperation,
    RepairTicketRecord,
)

#: 位置必须包含的量词/处所词，用来判断是否足以派单。
_LOCATION_HINTS = ("宿舍", "公寓", "楼", "室", "间", "栋", "舍", "寝", "校区", "园")
#: 无信息量的故障描述，去掉这些后若过短则视为未说明问题。
_EMPTY_PROBLEM_WORDS = ("坏了", "不行了", "有问题", "不能用了", "不好使", "故障", "报修", "修一下", "坏了啊")


class DormitoryError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def validate_location(location: str) -> str:
    """校验并规范化位置描述，返回去掉多余空白的值。"""
    value = (location or "").strip()
    if len(value) < 2:
        raise DormitoryError("MISSING_FIELDS", "请提供具体宿舍位置，例如：3 号楼 502 室")
    if not any(hint in value for hint in _LOCATION_HINTS) and not re.search(r"\d", value):
        raise DormitoryError(
            "INVALID_ARGUMENTS",
            "位置「%s」无法定位，请补充楼栋与房间号，例如：3 号楼 502 室" % value,
        )
    return re.sub(r"\s{2,}", " ", value)


def validate_problem(problem: str) -> str:
    """校验故障描述是否有实质内容。"""
    value = (problem or "").strip()
    if len(value) < 2:
        raise DormitoryError("MISSING_FIELDS", "请说明具体故障，例如：空调不制冷")
    stripped = value
    for word in _EMPTY_PROBLEM_WORDS:
        stripped = stripped.replace(word, "")
    stripped = re.sub(r"[\s，,。.、！!？?~～]+", "", stripped)
    if len(stripped) < 2:
        raise DormitoryError(
            "INVALID_ARGUMENTS",
            "故障描述「%s」信息不足，请说明具体现象，例如：空调不制冷、水龙头漏水" % value,
        )
    return value


class DormitoryService:
    def __init__(self, repository: CampusRepository, *, clock=None):
        self.repository = repository
        self._clock = clock or (lambda: datetime.now().astimezone())

    # ------------------------------------------------------------ 创建

    def create_ticket(self, *, user_id: str, operation_id: str,
                      location: str, problem: str) -> Dict[str, Any]:
        if not user_id or not user_id.strip():
            raise DormitoryError("UNAUTHORIZED", "缺少可信用户身份")
        if not operation_id or not operation_id.strip():
            raise DormitoryError("INVALID_ARGUMENTS", "缺少操作号")

        clean_location = validate_location(location)
        clean_problem = validate_problem(problem)

        key = {"user_id": user_id, "tool_name": "create_repair_ticket", "operation_id": operation_id}
        arguments = {"location": clean_location, "problem": clean_problem}
        try:
            cached = self.repository.begin_operation(arguments=arguments, **key)
        except DuplicateOperation as exc:
            raise DormitoryError("IDEMPOTENCY_KEY_REUSED", str(exc)) from exc
        if cached is not None:
            return cached

        # 业务唯一约束独立于幂等键：同一未完结工单不重复提交。
        existing = self.repository.find_open_ticket(
            user_id=user_id, location=clean_location, problem=clean_problem
        )
        if existing is not None:
            data = existing.to_public()
            data["duplicate"] = True
            # 缓存这条结果，使重复提交得到一致答复。
            self.repository.finish_operation(data=data, **key)
            return data

        now = self._clock()
        record = RepairTicketRecord(
            ticket_id="RP-" + uuid4().hex[:10].upper(),
            location=clean_location,
            problem=clean_problem,
            status="pending",
            user_id=user_id,
            created_at=now,
            updated_at=now,
            history=[{"status": "pending", "at": now.isoformat(), "note": "工单已创建"}],
        )
        self.repository.create_repair_ticket(record)

        data = record.to_public()
        data["simulated_note"] = "模拟工单，未通知学校维修人员"
        self.repository.finish_operation(data=data, **key)
        return data

    # ------------------------------------------------------------ 查询

    def get_status(self, *, user_id: str, ticket_id: str) -> Dict[str, Any]:
        record = self.repository.get_repair_ticket((ticket_id or "").strip())
        if record is None or record.user_id != user_id:
            raise DormitoryError("NOT_FOUND", "工单不存在或不属于当前用户")
        return record.to_public()

    def list_tickets(self, *, user_id: str) -> List[Dict[str, Any]]:
        records = self.repository.list_repair_tickets(user_id)
        records.sort(key=lambda r: r.created_at, reverse=True)
        return [r.to_public(include_history=False) for r in records]

    # ------------------------------------------------------------ 演示用状态更新

    def update_status(self, *, user_id: str, ticket_id: str, status: str,
                      note: str = "", allow_foreign: bool = False) -> Dict[str, Any]:
        """演示用状态流转。

        ``allow_foreign=False`` 时只允许操作本人工单；演示脚本以 ``True`` 模拟
        维修人员推进状态。非法流转返回 ``INVALID_STATUS_TRANSITION``。
        """
        record = self.repository.get_repair_ticket((ticket_id or "").strip())
        if record is None:
            raise DormitoryError("NOT_FOUND", "工单不存在")
        if not allow_foreign and record.user_id != user_id:
            raise DormitoryError("NOT_FOUND", "工单不存在或不属于当前用户")

        target = (status or "").strip()
        if target not in REPAIR_TRANSITIONS:
            raise DormitoryError(
                "INVALID_ARGUMENTS",
                "未知状态「%s」，可选：%s" % (target, "、".join(REPAIR_TRANSITIONS)),
            )
        if target not in REPAIR_TRANSITIONS[record.status]:
            raise DormitoryError(
                "INVALID_STATUS_TRANSITION",
                "工单当前为「%s」，不能改为「%s」" % (record.status, target),
            )

        now = self._clock()
        record.status = target
        record.updated_at = now
        record.history.append({"status": target, "at": now.isoformat(), "note": (note or "").strip()})
        self.repository.update_repair_ticket(record)
        return record.to_public()
