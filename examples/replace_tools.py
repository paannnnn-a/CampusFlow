"""给 B/C 的适配示例；本文件不假装已有真实接口。"""
from backend.agent.contracts import Context, DateArgs, ToolResult


def bind_schedule(registry, business_function):
    """传入 C 未来提供的 query_schedule(user_id, date) 同步函数。"""
    def adapter(args: DateArgs, context: Context):
        raw = business_function(user_id=context.user_id, date=args.date)
        # 如果 C 格式不同，在这里转换为 {success, data, message, error_code}。
        return ToolResult.model_validate(raw)
    registry.bind("get_schedule", adapter, simulated=True)


def bind_booking(registry, business_function):
    """C 在数据库事务内校验冲突，并保存 operation_id 幂等键。"""
    def adapter(args, context):
        return business_function(user_id=context.user_id,
                                 operation_id=context.operation_id,
                                 **args.model_dump(mode="json"))
    registry.bind("book_study_seat", adapter, simulated=True)

