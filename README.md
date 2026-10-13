# CampusFlow · A 主 Agent 模块

当前仓库包含 A 负责的主 Agent、工具编排和独立验收代码；B/C/D 模块待后续集成。

这是按《CampusFlow 校园智能服务 Agent 项目开发方案》和聊天中“先用 Mock 并行开发”的路线完成的 A 模块。你现在可以独立运行、演示、测试，不需要等待 B/C。后续把 Mock 函数替换为他们的业务函数即可。

## 已经完成

| A 的任务 | 本包实现 |
|---|---|
| 主 Agent 工作流 | 真正的 LangGraph StateGraph，规划、参数校验、确认、执行和循环路由 |
| 大模型接入 | Responses / Chat Completions；百炼模板、配置检查和真实工具调用自检 |
| 工具注册 | 13 个工具的 Pydantic 参数、统一返回协议、替换入口 |
| 多轮与复合任务 | 会话历史、当前轮状态、补问；课表→空闲→座位；知识→要求→草稿；活动→课表→报名 |
| 写操作确认 | 6 个写工具均通过 interrupt 暂停；确认编号、有效期、拒绝、恢复、重复确认拦截 |
| 失败处理 | 读工具最多 2 次尝试、模型最多 2 次瞬时重试；写工具不自动重试；循环上限；明确失败反馈 |
| 调用轨迹 | 参数、工具结果、耗时、尝试次数、操作号；结构化返回给 D |
| 验证和交接 | 94 项自动化测试、8 个行为验收场景、四场景演示及 B/C/D 文档 |

**边界：**所有校园业务都还是 Mock。记录存在内存，退出程序即丢失。没有连接学校系统、业务数据库、B 的 RAG 或 D 的 Web 页面。真实模型适配器已通过 HTTP 模拟测试，尚未使用你的 API Key 做在线验收。离线 demo 使用规则规划器，只支持本文示例及部分同义问法，不能代替大模型语义验收。

## 1. 安装与运行（Windows / PowerShell）

克隆本仓库后，在仓库根目录 `CampusFlow` 中打开终端。已验证环境为 Windows、Python 3.9.13；本包的依赖版本按该环境固定，建议团队统一环境后再升级。

```powershell
python --version
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-agent.txt
.\.venv\Scripts\python.exe run_agent.py --trace
```

不需要激活虚拟环境，也不需要设置 API Key。若 `python` 指向了其他环境，始终使用这里的 `.venv\Scripts\python.exe`。

输入：

```text
明天下午帮我找一个安静的地方学习两个小时，并预约座位。
```

程序会查询课表、计算空闲、查询座位，然后停在 `awaiting_confirmation`。此时尚未预约。

```text
/confirm
```

确认后才会创建模拟记录。也可输入 `/cancel` 拒绝。`/new` 开新会话，`/quit` 退出。存在待确认操作时修改需求，先 `/cancel` 再重新发送。

一次运行四个场景：

```powershell
.\.venv\Scripts\python.exe run_agent.py --demo-all --trace --now 2026-10-13T09:00:00+08:00
```

`--demo-all` 是离线验收脚本，会明确打印“模拟用户点击：确认”后调用确认入口；普通交互和 D 的服务入口均需显式确认。`--now` 固定演示时钟，方便复现，普通交互默认使用当前上海时间。

## 2. 你可以直接试的例子

| 场景 | 输入 | 后续输入 |
|---|---|---|
| 课表 | 今天有什么课 | 无 |
| 座位 | 明天下午学习两个小时并预约座位 | `/confirm` 或 `/cancel` |
| 校园事务 | 我的学生证丢了，怎么办？ | 生成草稿，姓名：张三，学号：20260001 → `/confirm` |
| 活动 | 下周有什么不与课表冲突的计算机讲座，帮我报名 | `/confirm` |
| 报修 | 我们宿舍空调不制冷，帮我报修 | 3号楼502室 → `/confirm` |
| 工单进度 | 查询工单状态 RP-实际返回的编号 | 无 |
| 取消预约 | 取消预约 RSV-实际返回的编号 | `/confirm` |
| 取消报名 | 取消报名 REG-实际返回的编号 | `/confirm` |

Mock 课表每天 09:00–10:00 有数据结构课，周一另有 14:00–16:00 高数课。活动种子中，一场周一讲座与高数冲突，另一场周二 18:00–20:00 无课，便于演示筛选。所有种子都仅用于开发。

## 3. 接入你们选用的大模型

**推荐的首轮接入：阿里云百炼 `qwen-plus`。** 已准备 `.env.aliyun.example`，按 [百炼接入步骤](docs/aliyun_setup.md) 创建 Key、配置、自检和验收。下面保留通用配置方式。

复制模板并填写：

```powershell
Copy-Item .env.example .env
```

编辑 `.env`：

```dotenv
LLM_API_KEY=你的密钥
LLM_MODEL=你的服务商实际提供且支持工具调用的模型名称
LLM_BASE_URL=https://api.openai.com/v1
LLM_API_STYLE=responses
```

官方 OpenAI 接口默认使用 `responses`；其他服务商若只支持 Chat Completions，将 `LLM_API_STYLE` 改为 `chat_completions` 并填写其 base URL（一般到 `/v1`，不要写到 `/chat/completions`）。没有预设模型名称，避免你们误用无权限或接口不兼容的模型。

```powershell
.\.venv\Scripts\python.exe run_agent.py --mode llm --trace
```

此时模型负责理解需求和选择工具，校园业务仍然使用 Mock。建议用上面四个场景进行真实模型验收；尤其检查来源引用、补问和活动冲突筛选。`.env` 已加入忽略规则，不要把密钥写入代码或发到群里。

模型工具调用的实现参考 [OpenAI 官方 Function calling 文档](https://developers.openai.com/api/docs/guides/function-calling)，Responses 历史保留参考 [官方 Reasoning 文档](https://developers.openai.com/api/docs/guides/reasoning)。LangGraph 确认流程采用 [官方 Interrupts 文档](https://docs.langchain.com/oss/python/langgraph/interrupts) 描述的暂停与恢复模式；具体运行行为已在本包固定版本下验证。

## 4. 运行测试

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

当前结果：**94 passed**。测试不调用外网，不消耗 API 额度。详见 [测试报告](docs/test_report.md)。

独立运行 8 场景行为验收：

```powershell
.\.venv\Scripts\python.exe evaluate_agent.py --mode demo
```

配置 `.env` 后验收真实模型：

```powershell
.\.venv\Scripts\python.exe check_llm.py
.\.venv\Scripts\python.exe check_llm.py --online
.\.venv\Scripts\python.exe evaluate_agent.py --mode llm
```

第一条不联网，后两条调用真实模型。校园业务仍使用全新 Mock；通过不代表 B/C/D 已完成集成。

## 5. 先读这些代码

```text
backend/
  agent/
    service.py          D 的入口：chat / confirm / inspect / history
    graph.py            LangGraph 节点、暂停与恢复
    state.py            会话状态
    planner.py          模型响应校验 + 离线规则演示器
    prompts.py          系统提示词与工具使用约束
    executor.py         参数校验、写操作拦截、有界重试
    tool_registry.py    13 个工具定义、绑定入口
    contracts.py        A 的候选协议，联调后交 D 统一
    mock_tools.py       A 专用 Mock，不代替 B/C 的业务层
  core/
    llm_client.py       两种模型 API 的 HTTP 适配器
examples/
  replace_tools.py      接入 B/C 函数的适配示例
tests/                 Agent、模型 HTTP、Mock 协议测试
docs/                  架构、协议、交接、测试和实际演示输出
run_agent.py            A 独立启动入口
check_llm.py            配置检查与可选在线连接自检
evaluate_agent.py       demo / llm 共用的固定行为验收
requirements-agent.txt  A 的依赖，合并时交 D 整合
```

## 6. 你接下来要做什么

1. 先运行离线四场景和测试，理解 `service.py → graph.py → executor.py`。
2. 把 [工具协议](docs/tool_contract.md) 和生成的 `docs/tool_schemas.json` 发给 B/C，把 [D 接口说明](docs/api_contract.md) 发给 D。这里只是可评审的协议草案，尚未代表团队已经确认冻结。
3. 团队确定服务商后填 `.env`，执行真实模型的四场景验收；记录模型选择和失败问法。
4. B/C 每交付一个函数，在 registry 中替换对应 Mock，保留其他 Mock 继续并行开发，见 `examples/replace_tools.py`。
5. 合入仓库时只引入 A 的代码与文档；`contracts.py` 由 D 评审后统一到公共 Schema，依赖由 D 整合。不要用本包覆盖 D 的启动器或公共配置。

交接清单和后续真实联调工作见 [A 交接说明](docs/handoff.md)。

本轮增量见 [第二版变更说明](docs/changes_v2.md)。
