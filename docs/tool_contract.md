# 给 B/C 的工具协议草案 v0.1

该协议是根据分工文档实现的可运行草案，供 ABCD 评审。还未和你们实际业务接口确认；接口名称不一致时用适配器转换，不需要修改 Agent 工作流。参数 JSON Schema 已生成在 `tool_schemas.json`。

## Python 绑定约定

```python
def handler(args: 对应Pydantic参数类型, context: Context) -> ToolResult | dict:
    ...

registry.bind("get_schedule", handler, simulated=True)
```

Python 3.9 类型提示用 `Union` 代替上面的说明性 `|` 写法。handler 必须是同步函数；如果业务只有异步接口，和 D 一起设计异步执行器后再接入。

`context.user_id` 由 D 的认证层提供；`context.operation_id` 由 A 针对本次调用生成。模型看不到、也不能指定这两个字段。A 不拼写 SQL。业务层必须再次检查记录归属，不能只检查模型传来的 ID。

## 统一返回

```json
{
  "success": true,
  "data": {},
  "message": "模拟操作成功",
  "error_code": null,
  "simulated": true,
  "retryable": false
}
```

`success/data/message/error_code` 延续分工文档；增加模拟标识与瞬时故障标识。`success` 必须是真正布尔值，`data` 必须是对象；没有记录时也不返回 null。成功不得带错误码；失败必须带错误码。`simulated/retryable` 可省略，默认 true/false。最终模拟属性由 registry 的绑定配置覆盖，避免业务返回误标官方成功。

`retryable=true` 仅在无副作用且可安全重试的读故障中使用。写操作无论该字段怎样设置，都不会由 A 自动重试。

## 各工具的 data

时间字段均为带时区 ISO 8601；A 会规范化为 `Asia/Shanghai`。日期为 `YYYY-MM-DD`。区间统一按半开区间 `[start_time, end_time)` 处理，首尾相接不冲突。

| 负责人 | 工具与模型参数 | 读写 | 成功 data 最少结构 |
|---|---|---|---|
| C | `get_schedule(date)` | 读 | `date, courses:[{course,start_time,end_time}]` |
| C | `find_free_slots(date,duration_minutes)` | 读 | `slots:[{start_time,end_time}]` |
| C | `find_available_seats(start_time,end_time)` | 读 | `seats:[{seat_id,location}], start_time,end_time` |
| C | `book_study_seat(seat_id,start_time,end_time)` | 写 | `reservation_id,seat_id,start_time,end_time,status` |
| C | `cancel_study_reservation(reservation_id)` | 写 | `reservation_id,status` |
| B | `search_campus_knowledge(query)` | 读 | `sources:[{source_id,title,content}]`，可扩展真实 URL/页码 |
| B | `get_affair_requirements(affair_type)` | 读 | `affair_type,required_fields,materials,steps` |
| B | `create_affair_draft(affair_type,fields)` | 写 | `draft_id,affair_type,fields,status` |
| C | `search_activities(keywords,start_time,end_time)` | 读 | `activities:[{activity_id,title,start_time,end_time,location}]` |
| C | `register_activity(activity_id)` | 写 | `registration_id,activity_id,status` |
| C | `cancel_activity_registration(registration_id)` | 写 | `registration_id,status` |
| B | `create_repair_ticket(location,problem)` | 写 | `ticket_id,location,problem,status` |
| B | `get_repair_status(ticket_id)` | 读 | `ticket_id,location,problem,status` |

本包统一验证返回信封；表格中的具体业务 data 由 B/C 按约定实现并测试，当前尚未对每个 data 定义单独输出模型。D 统一 Schema 时可以补充业务输出类型。

课表、活动、座位的空结果用空数组，属于成功查询；不能虚构替代数据。知识检索无依据时 `sources=[]`，明确说明未查到。Mock 的空闲范围为 08:00–22:00；正式业务的开放时段需要 C 与 A 再确认。

Mock 学生证补办 `fields` 采用 `name/student_id/reason`；正式业务的材料字段以 B 的 `required_fields` 为准，切换真实模型后由模型根据该结果追问。离线规则器仅演示固定字段。

## 必须由 B/C 保证的写入规则

1. 在数据库事务中重新检查时段冲突、重复报名和记录归属；查询到可用不代表确认时仍可用。
2. 把 `(user_id, tool_name, operation_id)` 用作唯一幂等键，保存结果；相同键返回原结果。
3. 预约和报名的业务唯一约束独立于操作号；模型换个操作号也不能重复占位或重复报名。
4. 如果相同幂等键的参数内容改变，应拒绝，而不是生成另一条记录。
5. 对未知结果的写入提供按操作号核实的方法。A 当前会在失败响应中给出此号；业务层的查询/人工处理方式待双方商定。
6. 返回明确错误码，例如 `RESERVATION_CONFLICT`、`COURSE_CONFLICT`、`DUPLICATE_REGISTRATION`、`MISSING_FIELDS`、`NOT_FOUND`。

Mock 的幂等缓存、锁和冲突检查只是单进程演示，不能代替上述数据库实现。

## A 的错误码

| 错误码 | 含义及客户端处理 |
|---|---|
| `INVALID_ARGUMENTS` | 缺少字段或类型/日期非法，向用户说明并让其补充新请求 |
| `UNKNOWN_TOOL` | 工具未注册或未绑定，检查 registry |
| `CONFIRMATION_REQUIRED` | 执行器拦截了未确认写操作 |
| `USER_CANCELLED` | 用户拒绝，不执行写入和本轮后续步骤 |
| `CONFIRMATION_EXPIRED` | 确认已超时，不执行写入，刷新后仍能查看原因 |
| `SKIPPED` | 批内前序失败，当前调用未执行；用于保持历史配对 |
| `TOOL_UNAVAILABLE` | 读服务瞬时失败，最多 2 次尝试 |
| `TOOL_ERROR` | 读工具异常或返回格式不符合协议 |
| `UNKNOWN_OUTCOME` | 写工具连接或异常后结果未知，先核实操作号，不自动重提 |
| `MODEL_ERROR` | 模型连接、返回格式、模型预算等错误，未新增工具执行 |
| `STEP_LIMIT` | 本轮达到规划/工具数上限 |

## 适配示例

```python
from backend.agent.contracts import ToolResult

def adapter(args, context):
    raw = c_service.query_schedule(user_id=context.user_id, date=args.date)
    # 按 C 的实际格式转换；这里假设 C 已遵守统一信封。
    return ToolResult.model_validate(raw)

registry.bind("get_schedule", adapter, simulated=True)
```

即使业务函数已写入团队的演示数据库，仍应 `simulated=True`。只有确实连接官方系统且业务操作真实生效，才讨论改成 false。
