# 第二版变更

继续完成 A 能独立推进的模型接入准备：

- `.env.aliyun.example`：百炼 qwen-plus 配置及非思考模式。
- `backend/core/llm_config.py`：本地配置检查，配置摘要不显示密钥。
- `check_llm.py`：默认离线检查，显式 `--online` 才检查真实工具调用和结果回传。
- `evaluate_agent.py`：8 场景行为验收，支持 demo/llm、单场景与 JSON 报告；每场景使用新建 Mock。
- 兼容服务的 reasoning_content 保留在模型协议中，不显示到用户响应。
- 基础模型调用失败时跳过后续验收，避免错误配置重复请求。

同时修复：确认超时原因现在持久保存在本次内存会话状态与轨迹中，刷新后答复一致；工具数恰好达上限时仍能生成最终答复；非法执行预算提前拒绝；模型 HTTP 错误保留安全状态码用于排查。

实际结果：**94 项测试通过，8 个离线验收场景通过**。机器结果见 `test_results.xml` 和 `evaluation_demo.json`。四场景演示见 `demo_output.txt`。

真实百炼请求尚未执行，等待本地 API Key。已有工具参数和 D 的公开响应格式保持兼容，错误码新增 `CONFIRMATION_EXPIRED`。B/C/D 真实业务和 Web 联调仍待完成。
