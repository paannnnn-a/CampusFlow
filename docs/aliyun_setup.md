# 阿里云百炼接入与验收

建议先用百炼 `qwen-plus` 联调。官方列出 Qwen-Plus 系列支持 Function Calling，且提供 OpenAI 兼容接口，本项目可复用现有代码。是否满足实际问法，用下文验收决定。[工具调用文档](https://help.aliyun.com/zh/model-studio/qwen-function-calling)、[兼容接口文档](https://help.aliyun.com/zh/model-studio/compatibility-of-openai-with-dashscope)。

## 1. 创建 API Key

登录 [百炼控制台](https://bailian.console.aliyun.com/)，按提示开通服务，选择华北 2（北京）地域，在 API Key 页面创建密钥。本模板使用北京接口，因此 Key 也要在同一地域创建。地域间的 Key、模型和接口不能混用。[官方创建步骤](https://help.aliyun.com/zh/model-studio/get-api-key)。

直接调用模型 API 即可；工具编排已经在 A 的 Python/LangGraph 代码里实现，不需要在百炼重新搭建一个低代码 Agent。

## 2. 填本地配置

在 `CampusFlow-A` 目录打开 PowerShell；已经安装依赖时跳过前两条命令。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-agent.txt
Copy-Item .env.aliyun.example .env
notepad .env
```

已有 `.env` 时直接编辑，避免模板覆盖现有配置。文件内容：

```dotenv
LLM_API_KEY=在这里填写百炼密钥
LLM_MODEL=qwen-plus
LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
LLM_API_STYLE=chat_completions
LLM_ENABLE_THINKING=false
```

模板使用北京原公共域名，官方说明仍可使用，同时建议迁移到业务空间专属域名。采用专属地址时改为 `https://你的业务空间ID.cn-beijing.maas.aliyuncs.com/compatible-mode/v1`，ID 从控制台查看；不要保留占位符。两种地址均只填到 `/compatible-mode/v1`，不要再加 `/chat/completions`。[地址与迁移说明](https://help.aliyun.com/zh/model-studio/compatibility-of-openai-with-dashscope)。

`enable_thinking=false` 用于先以非思考模式联调。官方 SDK 示例通过 extra_body 传入，本包直接使用 HTTPX，已将其作为 JSON 顶层字段发送，无需再安装 OpenAI SDK。[官方调用示例](https://help.aliyun.com/zh/model-studio/qwen-function-calling)。

也可用 `DASHSCOPE_API_KEY` 环境变量提供密钥。当前终端的环境变量优先于 `.env`；修改文件不生效时检查旧的 `LLM_*` 配置是否覆盖文件。

记事本打不开时，可直接运行 `python configure_key.py`，按提示粘贴密钥并回车。输入不显示字符是正常的；脚本只更新本地 `.env`，不会上传密钥。

## 3. 本地自检（不联网）

```powershell
.\.venv\Scripts\python.exe check_llm.py
```

检查必填配置、地址格式、占位符、接口类型和参数组合；只显示密钥是否已配置。通过仅代表格式正确，不证明 Key、权限或余额有效。

## 4. 真实连接与工具调用自检

```powershell
.\.venv\Scripts\python.exe check_llm.py --online
```

此步骤消耗少量模型 Token：正常流程为 2 次模型调用，每次瞬时故障最多尝试 2 次。先让模型调用无副作用测试函数，再回传结果，检查工具名称、随机参数、tool_call_id 配对与最终答复。能聊天但不返回结构化工具调用，不算通过。不查询或提交校园业务。

## 5. 校园场景验收

先跑一个场景，再跑完整 8 场景：

```powershell
.\.venv\Scripts\python.exe evaluate_agent.py --mode llm --case schedule_today
.\.venv\Scripts\python.exe evaluate_agent.py --mode llm
```

报告保存为 `docs/evaluation_llm.json`，检查课表工具选择、缺失日期补问、预约确认/拒绝、报修补问、知识引用、草稿字段与活动冲突。

每个场景使用新建的内存 Mock，脚本模拟用户点击确认；不会绑定 B/C 函数或触及学校系统。校园时钟固定为 `2026-10-13T09:00:00+08:00`，方便跨模型比较。真实 API 只负责规划和回复，业务写入仍在 Mock 中。

基础模型调用失败会停止后续场景，标记 skipped。查看失败项的 checks、参数与轨迹；调整提示词或配置后用 `--case` 单独重跑。结构化行为通过后，还应人工看解释、来源引用与取消反馈。

## 6. 交互体验

```powershell
.\.venv\Scripts\python.exe run_agent.py --mode llm --trace
```

试试“明天下午找个安静座位自习两小时并预约”“寝室空调不制冷帮我维修”等不同说法；待确认时输入 `/confirm` 或 `/cancel`。同义问法的真实模型能力需要配置 Key 后验收，固定场景的实际结果见 evaluation_llm.json，更多同义问法仍需验证。

## 常见问题

| 现象 | 检查方向 |
|---|---|
| 提示缺少 Key/模型 | `.env` 是否填写，文件名是否误为 `.env.txt` |
| HTTP 401/403 | Key 有效性、同地域、模型权限 |
| HTTP 404 | 地址、重复的接口路径、该地域模型是否可用 |
| HTTP 400 | 模型是否支持工具、非思考模式及非流式调用，参照官方错误说明 |
| HTTP 429 | 限流或额度限制，查看控制台后再试 |
| 能回复但没有工具调用 | 核对支持的模型与配置，不视作验收成功 |
| 基础连接通过但某场景失败 | 阅读 checks 和轨迹，定位规划或参数问题后重跑该 case |

计费和免费额度以你的百炼控制台与当前说明为准。本包未替你开通付费资源，也未承诺免费调用。
