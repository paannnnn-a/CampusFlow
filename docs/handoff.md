# A 的交接与后续工作

## 现在可以交付的部分

你可以将 `backend/agent/`、`backend/core/llm_client.py`、`backend/core/llm_config.py`、Agent 测试和本目录文档作为 A 的第一版工作提交到 `feature/agent`。`run_agent.py` 和 `requirements-agent.txt` 是独立开发辅助入口，避免覆盖 D 的 `run.py/requirements.txt`。

你已经可以演示主 Agent、13 个工具、会话补问、复合调用、写前确认、确认后恢复、失败/取消和轨迹。所有业务数据都明确标记模拟，退出即清空。

## 给队友的消息模板

> A 的 LangGraph 框架和 13 个 Mock 已可运行，四个基础场景通过。请 B/C 先评审 `docs/tool_contract.md` 和 `docs/tool_schemas.json`，确认参数、返回 data 和错误码；D 请评审 `docs/api_contract.md` 和响应 Schema。业务函数通过 `registry.bind` 逐个替换 Mock，用户身份与操作号由后端注入。所有写操作由 A 统一暂停确认，业务层继续负责冲突、归属和数据库幂等。协议目前是草案，需要大家确认后冻结。

## B/C 每交付一个工具时

1. 确认函数的输入、同步/异步方式、返回字段、错误码和超时。
2. 写薄适配器，把实际函数映射为 `handler(args, context)`。
3. `registry.bind` 替换该工具；其他工具继续使用 Mock。
4. 先运行 A 的测试；再用该真实业务实现做一次成功、一次参数/业务失败的联调。
5. 写工具额外确认：不点击无记录、点击后有记录、重复点击无重复记录、冲突时正确拒绝。
6. 保持 `simulated=True`，除非团队确实集成了学校官方系统。

## D 接入时

1. 合并依赖和 Pydantic 公共 Schema，避免同名类型分叉。
2. 应用生命周期复用一个 AgentService；认证上下文提供 user_id。
3. 包装 chat/confirm/inspect；前端显示轨迹与确认卡片。
4. 单进程先联调。再共同决定持久化、会话清理和多进程方案。
5. 将 Agent 内存记录替换为 B/C 的数据库服务；A 不承担 SQL 和数据库迁移。

## 第二版可直接使用的模型验收入口

阿里云配置见 `docs/aliyun_setup.md`。先用 `check_llm.py` 做离线检查，再显式 `--online` 检查工具回传，最后用 `evaluate_agent.py --mode llm` 运行固定校园场景；全部校园业务仍为新建 Mock。真实模型验收可以现在做，不需要等待 B/C。

## 尚不能宣称完成的验收

| 项目 | 原因及验收方式 |
|---|---|
| 真实模型自然语言能力 | 已准备百炼 qwen-plus 配置；尚未填入本地 API Key。配置后做在线验收 |
| 真实 RAG 检索质量 | 等 B 的资料、Embedding、检索与来源数据 |
| 数据库记录可持续查询 | 等 B/C 的业务写入和 D 的数据库基础设施 |
| 正式冲突约束和持久化幂等 | 需要业务数据库事务与唯一约束；Mock 锁只能证明本地流程 |
| Web 确认交互与端到端链路 | 等 D 的 API 与页面接入 |
| 多进程、重启恢复、长会话压缩 | 当前内存单进程版无此能力，按团队部署需要扩展 |

## A 建议的下一步顺序

先亲自跑一遍四场景，再阅读 `service.py → graph.py → executor.py`；和队友冻结第一版工具合同。确定模型配置后立即进行真实模型验收，不必等待 B/C。随后按工具交付顺序逐个替换适配器，最后参加 ABCD 全链路联调。

团队评审后再冻结接口协议。上述消息模板由你按团队沟通方式使用。
