# A 独立开发测试报告

测试日期：2026-10-13（Asia/Shanghai）。

环境：Windows，Python 3.9.13，LangGraph 0.2.76，Pydantic 2.10.6，HTTPX 0.28.1，pytest 8.3.5；全部在独立虚拟环境中安装后执行。

## 自动化结果

```text
python -m pytest -q
94 passed
```

| 文件 | 验证内容 |
|---|---|
| `tests/test_agent.py` | 四场景；多轮位置/故障与事务字段；多工具批次配对；逐项确认；取消、超时、错确认号；会话归属；重复及并发确认；读重试；写超时不重试；未知/非法工具；模型异常；循环上限；失败后报告既有成功；确认时座位冲突 |
| `tests/test_llm_client.py` | Chat Completions 工具结构；Responses 完整输出/推理项保留；call_id 结果配对；429/5xx 重试上限；401 不重试；错误不泄露服务商响应体；截断响应拒绝 |
| `tests/test_mock_contracts.py` | 13 工具正常返回；13 工具非法身份/缺失参数拒绝；重复报名、课表冲突、草稿字段、查询归属、知识空结果；严格布尔信封；同一幂等键参数改变时拒绝 |
| `tests/test_llm_config.py` | 配置必填项、地址、占位符与参数组合；密钥摘要脱敏；DashScope 别名；本地检查不构造网络客户端 |
| `tests/test_evaluation.py` | 8 个离线行为场景；纯文本不能伪装工具成功；基础模型调用失败后跳过其余场景 |

测试使用真实 LangGraph 节点、中断和 checkpoint，不替换成手工 if/else 的工作流。模型语义相关测试使用规则规划器或脚本模型；HTTP 测试使用 `httpx.MockTransport`，无外网调用。

第二版增加百炼参数、协议字段保留、连接自检、超时刷新一致性、工具预算边界与非法预算测试。机器可读的实际 pytest 结果见 `test_results.xml`。

独立执行 `python evaluate_agent.py --mode demo`：**8 passed、0 failed、0 skipped**，详见 `evaluation_demo.json`。同一脚本的 `--mode llm` 可验收真实模型，尚待本地 API Key。

## 四场景实际运行

```powershell
python run_agent.py --demo-all --trace --now 2026-10-13T09:00:00+08:00
```

实际输出保存在 `demo_output.txt`，所有场景先暂停等待确认，再由批量示例模拟用户点击：

| 场景 | 观察结果 |
|---|---|
| 学习座位 | 查 2026-10-14 课表 → 空闲 → 查座位 → 待确认 → 模拟预约，生成 RSV 编号 |
| 学生证 | 模拟政策来源引用 → 材料 → 补充姓名学号 → 待确认 → 模拟草稿，生成 DFT 编号 |
| 活动 | 检索下周讲座 → 查周一、周二课表 → 排除周一冲突 → 待确认 → 报名 ACT-FREE，生成 REG 编号 |
| 报修 | 首轮位置补问 → 保留空调不制冷故障 → 待确认 → 模拟工单，生成 RP 编号 |

## 验证范围

上述结果证明 A 可以在 B/C 接口就绪前独立运行与验证编排。没有在线验证真实模型、真实 RAG、校园官方服务、业务数据库或 D 的 Web 页面；不能将“94 项单元测试通过”视为整个 CampusFlow 已完成端到端验收。真实 API、数据库并发约束和前端确认仍需集成阶段验收。
