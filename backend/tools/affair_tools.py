"""校园事务工具：``get_affair_requirements`` 与 ``create_affair_draft``。

对应 ``tool_contract.md``：
- ``get_affair_requirements(affair_type)`` → 读，``affair_type/required_fields/materials/steps``
- ``create_affair_draft(affair_type, fields)`` → 写，``draft_id/affair_type/fields/status``

扩展字段：``required_field_labels``（中文标签）、``source`` / ``source_type``
（标明办理要求的来源与模拟属性），前端与模型据此说明依据。
"""
from __future__ import annotations

from typing import Any, Dict

from ..agent.contracts import Context, AffairArgs, DraftArgs
from ..services.affairs import AffairError, AffairService
from ._common import BusinessError, read_tool, write_tool


def _as_business_error(exc: AffairError) -> BusinessError:
    return BusinessError(exc.code, exc.message)


class AffairTools:
    def __init__(self, service: AffairService):
        self.service = service
        self.get_affair_requirements = read_tool(self._requirements)
        self.create_affair_draft = write_tool(self._create_draft)

    # ------------------------------------------------------------ 读

    def _requirements(self, args: AffairArgs, context: Context):
        try:
            data = self.service.get_requirements(args.affair_type)
        except AffairError as exc:
            raise _as_business_error(exc) from exc

        message = "已找到「%s」的办理要求" % data["affair_type"]
        if data.get("source_type") == "simulated":
            message += "（模拟整理资料，须告知用户以学校实际通知为准）"
        return data, message

    # ------------------------------------------------------------ 写

    def _create_draft(self, args: DraftArgs, context: Context):
        # user_id 与 operation_id 来自可信上下文，绝不从模型参数读取。
        try:
            data = self.service.create_draft(
                user_id=context.user_id,
                operation_id=context.operation_id,
                affair_type=args.affair_type,
                fields=args.fields,
            )
        except AffairError as exc:
            raise _as_business_error(exc) from exc

        if data.get("duplicate"):
            message = "该操作已处理过，返回原草稿：%s" % data["draft_id"]
        else:
            message = "已保存模拟申请草稿 %s，尚未向学校正式提交" % data["draft_id"]
        return data, message

    # ------------------------------------------------------------ 绑定

    def bind(self, registry) -> None:
        registry.bind("get_affair_requirements", self.get_affair_requirements, simulated=True)
        registry.bind("create_affair_draft", self.create_affair_draft, simulated=True)
