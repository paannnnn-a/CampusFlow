"""宿舍报修工具：``create_repair_ticket`` 与 ``get_repair_status``。

对应 ``tool_contract.md``：
- ``create_repair_ticket(location, problem)`` → 写，``ticket_id/location/problem/status``
- ``get_repair_status(ticket_id)`` → 读，``ticket_id/location/problem/status``

扩展字段：``created_at`` / ``updated_at`` / ``history``（状态流转记录）、
``duplicate``（是否命中已有未完结工单）、``simulated_note``（模拟属性说明）。
"""
from __future__ import annotations

from ..agent.contracts import Context, RepairArgs, TicketArgs
from ..services.dormitory import DormitoryError, DormitoryService
from ._common import BusinessError, read_tool, write_tool

#: status 的中文展示，供 message 使用，便于直接进入答复。
STATUS_LABELS = {
    "pending": "待受理",
    "accepted": "已受理",
    "processing": "维修中",
    "resolved": "已完成",
    "cancelled": "已取消",
}


def _label(status: str) -> str:
    return STATUS_LABELS.get(status, status)


def _as_business_error(exc: DormitoryError) -> BusinessError:
    return BusinessError(exc.code, exc.message)


class DormitoryTools:
    def __init__(self, service: DormitoryService):
        self.service = service
        self.create_repair_ticket = write_tool(self._create)
        self.get_repair_status = read_tool(self._status)

    # ------------------------------------------------------------ 写

    def _create(self, args: RepairArgs, context: Context):
        try:
            data = self.service.create_ticket(
                user_id=context.user_id,
                operation_id=context.operation_id,
                location=args.location,
                problem=args.problem,
            )
        except DormitoryError as exc:
            raise _as_business_error(exc) from exc

        if data.get("duplicate"):
            message = "该位置同一故障已有未完结工单 %s（%s），未重复创建" % (
                data["ticket_id"], _label(data["status"]))
        else:
            message = "已创建模拟报修工单 %s，当前状态：%s；未通知学校维修人员" % (
                data["ticket_id"], _label(data["status"]))
        return data, message

    # ------------------------------------------------------------ 读

    def _status(self, args: TicketArgs, context: Context):
        try:
            data = self.service.get_status(user_id=context.user_id, ticket_id=args.ticket_id)
        except DormitoryError as exc:
            raise _as_business_error(exc) from exc
        return data, "工单 %s 当前状态：%s" % (data["ticket_id"], _label(data["status"]))

    # ------------------------------------------------------------ 绑定

    def bind(self, registry) -> None:
        registry.bind("create_repair_ticket", self.create_repair_ticket, simulated=True)
        registry.bind("get_repair_status", self.get_repair_status, simulated=True)
