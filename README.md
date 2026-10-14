# CampusFlow

面向校园生活场景的服务智能体。当前仓库实现主 Agent 与工具编排模块，覆盖课表与空间资源、校园事务、活动和宿舍报修四类服务。

## 功能与状态

- LangGraph 工作流：工具选择、多轮补问、复合任务、执行轨迹和失败处理。
- 13 个工具接口，Pydantic 参数及返回协议校验。
- 预约、取消、报名、申请草稿和报修等写操作逐项确认后执行。
- 支持 Responses 与 Chat Completions 模型接口，以及离线规则演示。

业务工具目前使用 Mock，记录和会话保存在单进程内存中，重启后清空。业务数据库、RAG、Web 前端和真实服务接口待集成；真实模型流程仍需在线联调验收。

## 目录

| 路径 | 内容 |
|---|---|
| `backend/agent/` | 工作流、状态、提示词、执行器、工具注册及 Mock |
| `backend/core/` | 模型客户端与配置校验 |
| `tests/` | Agent、工具协议、模型适配及行为验收测试 |
| `examples/` | 业务工具适配示例与 Schema 导出 |
| `docs/architecture.md` | 工作流、确认机制和运行约束 |

服务入口为 `backend/agent/service.py` 中的 `AgentService`；参数与响应类型定义在 `backend/agent/contracts.py`，工具定义在 `backend/agent/tool_registry.py`。

## 运行

已验证环境：Python 3.9，依赖版本见 `requirements-agent.txt`。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-agent.txt
.\.venv\Scripts\python.exe run_agent.py --mode demo --trace
```

真实模型配置使用 `.env.example` 或 `.env.aliyun.example`，配置写入本地 `.env` 后运行：

```powershell
.\.venv\Scripts\python.exe run_agent.py --mode llm --trace
```

交互命令：`/confirm` 确认、`/cancel` 拒绝待执行操作、`/new` 新会话、`/quit` 退出。

## 验证

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe evaluate_agent.py --mode demo
```

`check_llm.py` 提供本地配置检查，`--online` 检查模型工具调用与结果回传。`evaluate_agent.py --mode llm` 运行真实模型行为验收，校园业务仍使用 Mock。

接口 Schema 可通过 `python -m examples.export_schemas` 从代码生成。生成的报告和 Schema 不纳入版本控制。
