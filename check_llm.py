"""默认只检查本地配置；显式 --online 才发送最多两次模型请求。"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from uuid import uuid4

from dotenv import load_dotenv

from backend.core.llm_client import HTTPModelClient, ModelError
from backend.core.llm_config import LLMConfig
from backend.agent.planner import normalize_reply


PROBE_TOOL = {"type": "function", "function": {
    "name": "campusflow_probe", "description": "连接测试：按原样返回 nonce。不访问学校系统。",
    "parameters": {"type": "object", "properties": {"nonce": {"type": "string"}},
                   "required": ["nonce"], "additionalProperties": False}}}


def probe(model):
    nonce = uuid4().hex
    messages = [{"role": "user", "content": "请调用 campusflow_probe，参数 nonce 为 %s；获得工具结果后简短说明连接成功。" % nonce}]
    system = "这是工具调用兼容性测试。必须调用提供的工具，不能自行模拟工具结果。"
    message, calls = normalize_reply(model.complete(messages, [PROBE_TOOL], system=system, state={}))
    if len(calls) != 1 or calls[0]["function"]["name"] != "campusflow_probe":
        raise ModelError("模型未返回预期工具调用；请确认模型支持 Function Calling，再运行完整验收")
    try:
        arguments = json.loads(calls[0]["function"]["arguments"])
    except ValueError as exc:
        raise ModelError("模型工具参数不是有效 JSON") from exc
    if arguments != {"nonce": nonce}:
        raise ModelError("模型未正确保留工具参数")
    messages += [message, {"role": "tool", "tool_call_id": calls[0]["id"],
                          "content": json.dumps({"success": True, "data": {"nonce": nonce}})}]
    final, remaining = normalize_reply(model.complete(messages, [PROBE_TOOL], system=system, state={}))
    if remaining or not final["content"].strip():
        raise ModelError("工具结果回传后未得到最终文本答复")
    return {"tool_call": "passed", "tool_result_roundtrip": "passed", "requests": 2}


def main(argv=None):
    parser = argparse.ArgumentParser(description="CampusFlow 模型配置与工具调用自检")
    parser.add_argument("--online", action="store_true", help="发送真实模型请求，会消耗少量 Token")
    args = parser.parse_args(argv)
    load_dotenv(Path(__file__).with_name(".env"), override=False)
    try:
        config = LLMConfig.from_mapping(os.environ)
    except ValueError as exc:
        print("配置未就绪：" + str(exc))
        print("阿里云：复制 .env.aliyun.example 为 .env，填写同地域 API Key。未发送网络请求。")
        return 2
    print(json.dumps(config.safe_summary(), ensure_ascii=False, indent=2))
    if not args.online:
        print("本地配置检查通过；未联网。用 --online 检查真实工具调用。")
        return 0
    model = HTTPModelClient.from_env()
    try:
        print(json.dumps(probe(model), ensure_ascii=False, indent=2))
        print("基础连接及工具结果回传通过。继续运行 evaluate_agent.py --mode llm 验收校园场景。")
        return 0
    except (ModelError, ValueError):
        print("在线自检失败。请按 docs/aliyun_setup.md 检查 Key、地域、模型权限、余额和接口类型。")
        return 1
    finally:
        model.close()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
