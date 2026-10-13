# 给 D 的 Agent 服务接口草案

A 已提供 Python 入口，D 在自己的 FastAPI 层包装，不需要复制 Agent 逻辑。状态响应 Schema 见 `agent_response.schema.json`。

```python
from backend.agent.service import AgentService

# 应用启动时仅创建一次。正式联调时传入模型客户端和已绑定的业务 registry。
agent = AgentService.demo()

r = agent.chat(user_id="由认证上下文提供", message="明天下午学习两个小时并预约座位")
# r.session_id 保存到前端；不要为同一聊天每次都建立新 session。
if r.pending_confirmation:
    r = agent.confirm(
        user_id="同一认证用户",
        session_id=r.session_id,
        confirmation_id=r.pending_confirmation.confirmation_id,
        approved=True,  # 必须来自用户点击，不能由模型输出决定
    )

payload = r.model_dump(mode="json")
```

`completed` 表示本轮已答复，也可能是向用户补问；用户补充信息时继续调用 `chat` 并携带相同 `session_id`。`awaiting_confirmation` 表示已暂停，尚未写入。`cancelled` 和 `failed` 都已停止本轮，已有成功写入不会自动回滚。

## 建议 HTTP 路由（由 D 实现）

| 路由 | 请求体 | 调用 |
|---|---|---|
| `POST /api/chat` | `message, session_id?` | `agent.chat(...)` |
| `POST /api/chat/confirm` | `session_id, confirmation_id, approved` | `agent.confirm(...)` |
| `GET /api/chat/{session_id}` | 无 | `agent.inspect(...)` |

请求体不接收 `user_id`。D 从演示登录态或真正认证中取得它；对实际不存在/越权会话统一处理，避免泄漏会话信息。不得把任意请求体中的用户编号直接当作可信身份。

服务是同步调用，在异步 FastAPI 路由内使用线程池，避免阻塞事件循环：

```python
from starlette.concurrency import run_in_threadpool

# 位于 D 自己的 async 路由中；body 和 current_user 是 D 提供的已校验对象。
result = await run_in_threadpool(
    agent.chat,
    user_id=current_user.id,
    session_id=body.session_id,
    message=body.message,
)
return result.model_dump(mode="json")
```

使用 Pydantic `StrictBool` 校验确认请求的 `approved`，不要把字符串 `"false"` 转成 truthy 值。预期 `ValueError` 转为受控的 4xx；无待确认、编号不匹配、重复确认建议 409；身份或会话归属失败按 D 的认证方案返回 404/403。不要原样暴露堆栈。

## 响应示例

```json
{
  "session_id": "后端生成的会话编号",
  "status": "awaiting_confirmation",
  "answer": "请核对以下模拟操作，确认后执行……",
  "pending_confirmation": {
    "confirmation_id": "后端生成的单次确认编号",
    "tool_name": "book_study_seat",
    "arguments": {
      "seat_id": "LIB-A301-01",
      "start_time": "2026-10-14T14:00:00+08:00",
      "end_time": "2026-10-14T16:00:00+08:00"
    },
    "expires_at": "2026-10-13T09:05:00+08:00",
    "simulated": true,
    "message": "请核对工具名称和全部参数，确认后才执行。"
  },
  "trace": []
}
```

此例省略了前置读操作的轨迹。实际 `trace` 每项包含 `tool_name/call_id/operation_id/arguments/result/attempts/duration_ms`，只包含本轮业务摘要，不包含模型内部思维链。确认恢复后的响应会保留本轮之前已完成的轨迹。

## 确认 UI

- 展示清晰的动作名称、全部参数、模拟标记和有效期。活动报名可从此前查询轨迹取得活动标题与时间。
- 确认按钮只发送当前 `confirmation_id` 与布尔决定；不要回传编辑后的工具参数，也不要让模型生成批准结果。
- 一次确认只批准一个写调用；响应仍为 `awaiting_confirmation` 时，要展示下一项新的确认卡片。
- 待确认时禁止发送新聊天；用户想改参数时先取消，再提交新消息。
- 按钮防抖后仍保留后端重复确认校验。页面刷新可以调用 inspect 重新取得待确认状态。
- “取消本次提交”和“取消已有预约”是两种动作：前者拒绝待确认，不调用业务；后者调用取消工具，也要再次确认。

## 生命周期约束

内存版只支持单进程，暂不支持重启后恢复，也不支持多个 worker 共享会话。D 需要将 `AgentService` 放在应用生命周期或 `app.state` 中，而不是按请求构造。持久化 checkpoint、会话所有权和分布式锁需在团队集成阶段一同设计。

`history()` 只供后端调试与后续存储适配，包含模型协议字段和工具输出。D 的聊天 UI 应筛选用户/最终答复，单独显示 `trace`；不要直接把原始完整历史当成聊天气泡返回给前端。
