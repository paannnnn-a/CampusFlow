"""LangGraph 编排：模型规划→参数校验→写操作 interrupt→执行→继续规划。"""
from __future__ import annotations

import json
from datetime import timedelta
from uuid import uuid4

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from backend.core.llm_client import ModelError

from .contracts import PendingConfirmation, Trace, fail
from .executor import Executor
from .planner import normalize_reply
from .prompts import SYSTEM_PROMPT
from .state import AgentState


def result_message(call_id, result):
    return {"role": "tool", "tool_call_id": call_id, "content": result.model_dump_json()}


def failure_answer(trace, result):
    text = "本次处理已停止：%s（%s）。" % (result.message, result.error_code)
    successful_writes = [t for t in trace if t["result"]["success"] and t["tool_name"] in {
        "book_study_seat", "cancel_study_reservation", "create_affair_draft", "register_activity",
        "cancel_activity_registration", "create_repair_ticket"}]
    if successful_writes:
        text += "\n此前已完成的操作未回滚：\n" + json.dumps([
            {"tool": t["tool_name"], "data": t["result"]["data"], "simulated": t["result"]["simulated"]}
            for t in successful_writes], ensure_ascii=False)
    if result.error_code == "UNKNOWN_OUTCOME":
        text += "\n请让业务方核实操作号：" + trace[-1]["operation_id"]
    return text


def build_graph(model, registry, *, clock, checkpointer=None, max_rounds=10,
                max_tools=32, confirmation_ttl=300, read_attempts=2):
    for value in [max_rounds, max_tools, confirmation_ttl, read_attempts]:
        if type(value) is not int or value < 1:
            raise ValueError("步数、确认有效期和尝试次数必须为正整数")
    executor = Executor(registry, read_attempts=read_attempts)

    def plan(state):
        if state["rounds"] >= max_rounds:
            text = failure_answer(state["trace"], fail("STEP_LIMIT", "已达到本轮处理步数上限，请缩小任务范围"))
            return {"status": "failed", "answer": text, "messages": state["messages"] + [{"role": "assistant", "content": text}]}
        try:
            message, calls = normalize_reply(model.complete(
                state["messages"], registry.schemas(), system=SYSTEM_PROMPT.format(now=state["now"]), state=state))
            if len(calls) > max_tools - state["tool_count"]:
                raise ValueError("本轮工具调用数量超过限制")
        except Exception as exc:
            # 不回显服务商异常，避免泄露请求或密钥。上层可通过 trace 核实已完成操作。
            detail = str(exc) if isinstance(exc, ModelError) else "模型调用失败或返回格式异常；请检查模型配置，稍后重试"
            text = failure_answer(state["trace"], fail("MODEL_ERROR", detail))
            return {"status": "failed", "answer": text, "messages": state["messages"] + [{"role": "assistant", "content": text}]}
        return {"messages": state["messages"] + [message], "calls": calls, "pointer": 0,
                "rounds": state["rounds"] + 1, "status": "running" if calls else "completed",
                "answer": "" if calls else message["content"]}

    def dispatch(state):
        if state["status"] in {"completed", "failed", "cancelled"}:
            return END
        return "prepare" if state["pointer"] < len(state["calls"]) else "plan"

    def prepare(state):
        call = state["calls"][state["pointer"]]
        args, error = executor.prepare(call)
        name = call["function"]["name"]
        current = {"id": call["id"], "name": name, "arguments": args or {}, "operation_id": uuid4().hex}
        pending = None
        if error:
            current["error"] = {"code": error.error_code, "message": error.message}
        elif registry.specs[name].write:
            pending = PendingConfirmation(confirmation_id=uuid4().hex, tool_name=name, arguments=args,
                expires_at=clock() + timedelta(seconds=confirmation_ttl), simulated=registry.is_simulated(name)).model_dump(mode="json")
        return {"current": current, "pending": pending, "approved": False, "approval_expired": False}

    def after_prepare(state):
        return "approve" if state["pending"] else "execute"

    def approve(state):
        # interrupt 会重入本节点；所有随机 ID 和参数在前置节点中生成并已 checkpoint。
        decision = interrupt(state["pending"])
        valid = isinstance(decision, dict) and decision.get("confirmation_id") == state["pending"]["confirmation_id"]
        expired = clock() >= PendingConfirmation.model_validate(state["pending"]).expires_at
        return {"approved": valid and decision.get("approved") is True and not expired, "approval_expired": expired}

    def execute(state):
        current = state["current"]
        denied = bool(state["pending"]) and not state["approved"]
        if denied:
            code = "CONFIRMATION_EXPIRED" if state["approval_expired"] else "USER_CANCELLED"
            detail = "确认已超时，操作已取消；请重新发起请求" if state["approval_expired"] else "已取消当前待确认操作及本轮后续步骤"
            entry = Trace(tool_name=current["name"], call_id=current["id"], operation_id=current["operation_id"],
                          arguments=current["arguments"], result=fail(code, detail).model_copy(
                              update={"simulated": registry.is_simulated(current["name"])}), attempts=0, duration_ms=0)
        else:
            entry = executor.run(current, user_id=state["user_id"], approved=state["approved"])
        messages = state["messages"] + [result_message(current["id"], entry.result)]
        trace = state["trace"] + [entry.model_dump(mode="json")]
        changes = {"messages": messages, "trace": trace, "pointer": state["pointer"] + 1,
                   "tool_count": state["tool_count"] + 1, "pending": None, "current": None, "approved": False, "approval_expired": False}
        if not entry.result.success:
            # 一次模型响应可能包含多个调用。停止时仍补齐 tool_call_id，保持下轮协议有效。
            for remaining in state["calls"][state["pointer"] + 1:]:
                messages.append(result_message(remaining["id"], fail("SKIPPED", "前序失败或用户取消，未执行")))
            text = failure_answer(trace, entry.result)
            messages.append({"role": "assistant", "content": text})
            changes.update(status="cancelled" if denied else "failed", answer=text)
        return changes

    builder = StateGraph(AgentState)
    for name, node in [("plan", plan), ("prepare", prepare), ("approve", approve), ("execute", execute)]:
        builder.add_node(name, node)
    builder.add_edge(START, "plan")
    builder.add_conditional_edges("plan", dispatch, {"prepare": "prepare", "plan": "plan", END: END})
    builder.add_conditional_edges("prepare", after_prepare, {"approve": "approve", "execute": "execute"})
    builder.add_edge("approve", "execute")
    builder.add_conditional_edges("execute", dispatch, {"prepare": "prepare", "plan": "plan", END: END})
    return builder.compile(checkpointer=checkpointer or MemorySaver())

