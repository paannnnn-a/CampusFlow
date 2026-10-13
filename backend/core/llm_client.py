"""可切换 Responses / Chat Completions 的 HTTP 适配器，无隐式 SDK 重试。"""
from __future__ import annotations

import os
import time
from typing import Protocol

import httpx

from .llm_config import LLMConfig


class ModelError(RuntimeError):
    pass


class ModelClient(Protocol):
    def complete(self, messages, tools, *, system, state):
        ...


class HTTPModelClient:
    def __init__(self, *, api_key, model, base_url="https://api.openai.com/v1",
                 api_style="responses", http_client=None, max_attempts=2, enable_thinking=None):
        self.config = LLMConfig(api_key=api_key, model=model, base_url=base_url,
                                api_style=api_style, enable_thinking=enable_thinking)
        if type(max_attempts) is not int or max_attempts < 1:
            raise ValueError("max_attempts 必须为正整数")
        self.api_key, self.model = api_key, model
        self.base_url, self.api_style = base_url.rstrip("/"), api_style
        self.http = http_client or httpx.Client(timeout=httpx.Timeout(45, connect=10))
        self.max_attempts = max_attempts

    @classmethod
    def from_env(cls):
        config = LLMConfig.from_mapping(os.environ)
        return cls(api_key=config.api_key, model=config.model, base_url=config.base_url,
                   api_style=config.api_style, enable_thinking=config.enable_thinking)

    def close(self):
        self.http.close()

    def complete(self, messages, tools, *, system, state):
        if self.api_style == "responses":
            inputs = []
            for message in messages:
                if message["role"] == "tool":
                    inputs.append({"type": "function_call_output", "call_id": message["tool_call_id"], "output": message["content"]})
                elif message.get("_response_items"):
                    # 保留 reasoning 等完整输出项，供需要此信息的模型继续工具调用。
                    inputs.extend(message["_response_items"])
                else:
                    inputs.append({"role": message["role"], "content": message.get("content") or ""})
            payload = {"model": self.model, "instructions": system, "input": inputs, "store": False,
                       "tools": [{"type": "function", **t["function"], "strict": False} for t in tools]}
            path = "/responses"
        else:
            clean = []
            for message in messages:
                outgoing = {k: v for k, v in message.items() if not k.startswith("_")}
                # 部分兼容服务的思考模式要求保留此字段；不显示到用户界面。
                outgoing.update(message.get("_chat_fields", {}))
                clean.append(outgoing)
            payload = {"model": self.model, "messages": [{"role": "system", "content": system}] + clean,
                       "tools": tools, "tool_choice": "auto"}
            if self.config.enable_thinking is not None:
                payload["enable_thinking"] = self.config.enable_thinking
            path = "/chat/completions"
        response = self._post(path, payload)
        try:
            raw = response.json()
            if self.api_style == "chat_completions":
                choice = raw["choices"][0]
                if choice.get("finish_reason") not in {"stop", "tool_calls"}:
                    raise ValueError("截断或不受支持的结束原因")
                message = choice["message"]
                # 兼容服务可能在非流式 tool_calls 中也附带 index 等传输字段。
                # 只将协议必需字段交给严格的 Agent 工具调用模型。
                calls = [{"id": tool["id"], "type": tool["type"], "function": {
                    "name": tool["function"]["name"], "arguments": tool["function"]["arguments"]}}
                    for tool in message.get("tool_calls") or []]
                parsed = {"role": "assistant", "content": message.get("content") or "",
                          "tool_calls": calls}
                if isinstance(message.get("reasoning_content"), str):
                    parsed["_chat_fields"] = {"reasoning_content": message["reasoning_content"]}
                return parsed
            if raw.get("status") != "completed":
                raise ValueError("响应未完整结束")
            items = raw["output"]
            calls = [{"id": item["call_id"], "type": "function", "function": {
                "name": item["name"], "arguments": item["arguments"]}}
                for item in items if item["type"] == "function_call"]
            content = "\n".join(part["text"] for item in items if item["type"] == "message"
                                for part in item.get("content", []) if part["type"] == "output_text")
            return {"role": "assistant", "content": content, "tool_calls": calls, "_response_items": items}
        except (KeyError, IndexError, ValueError, TypeError) as exc:
            raise ModelError("模型响应格式异常、被截断或未完成；未执行新的工具") from exc

    def _post(self, path, payload):
        for attempt in range(self.max_attempts):
            try:
                response = self.http.post(self.base_url + path, json=payload,
                                          headers={"Authorization": "Bearer " + self.api_key})
            except httpx.TransportError as exc:
                if attempt + 1 == self.max_attempts:
                    kind = type(exc).__name__
                    detail = "模型请求超时" if isinstance(exc, httpx.TimeoutException) else "模型网络连接失败"
                    raise ModelError("%s（%s），请稍后重试" % (detail, kind)) from exc
            else:
                if response.is_success:
                    return response
                if response.status_code not in {429, 500, 502, 503, 504} or attempt + 1 == self.max_attempts:
                    raise ModelError("模型服务请求失败（HTTP %d），请检查配置或稍后重试" % response.status_code)
            time.sleep(0.1 * (2 ** attempt))

