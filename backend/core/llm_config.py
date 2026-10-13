"""本地配置校验；错误和摘要不包含 API Key。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Optional

import httpx


@dataclass(frozen=True)
class LLMConfig:
    api_key: str = field(repr=False)
    model: str
    base_url: str = "https://api.openai.com/v1"
    api_style: str = "responses"
    enable_thinking: Optional[bool] = None

    def __post_init__(self):
        if not isinstance(self.api_key, str) or not self.api_key.strip():
            raise ValueError("未设置 LLM_API_KEY（也可使用 DASHSCOPE_API_KEY）")
        if not self.model.strip():
            raise ValueError("未设置 LLM_MODEL")
        if self.api_style not in {"responses", "chat_completions"}:
            raise ValueError("LLM_API_STYLE 必须为 responses 或 chat_completions")
        if any(c.isspace() for c in self.base_url) or any(c in self.base_url for c in "{}<>"):
            raise ValueError("LLM_BASE_URL 含空格或未替换的占位符")
        try:
            url = httpx.URL(self.base_url)
            valid = url.scheme in {"https", "http"} and bool(url.host)
        except Exception:
            valid = False
            url = None
        if not valid:
            raise ValueError("LLM_BASE_URL 必须是完整 http(s) 地址")
        if url.username or url.password or url.query or url.fragment:
            raise ValueError("LLM_BASE_URL 不应包含账号、密码、查询参数或片段")
        if self.base_url.rstrip("/").endswith(("/chat/completions", "/responses")):
            raise ValueError("LLM_BASE_URL 应填写接口根地址，不要包含 /chat/completions 或 /responses")
        if self.enable_thinking is not None and type(self.enable_thinking) is not bool:
            raise ValueError("enable_thinking 必须为布尔值")
        if self.enable_thinking is not None and self.api_style != "chat_completions":
            raise ValueError("LLM_ENABLE_THINKING 只用于支持该参数的 Chat Completions 服务；官方 Responses 请留空")

    @classmethod
    def from_mapping(cls, env: Mapping[str, str]):
        raw = env.get("LLM_ENABLE_THINKING", "").strip().lower()
        if raw not in {"", "true", "false"}:
            raise ValueError("LLM_ENABLE_THINKING 仅支持 true、false 或留空")
        return cls(api_key=env.get("LLM_API_KEY", "").strip() or env.get("DASHSCOPE_API_KEY", "").strip(),
                   model=env.get("LLM_MODEL", "").strip(),
                   base_url=env.get("LLM_BASE_URL", "https://api.openai.com/v1").strip().rstrip("/"),
                   api_style=env.get("LLM_API_STYLE", "responses").strip(),
                   enable_thinking=None if raw == "" else raw == "true")

    def safe_summary(self):
        return {"api_key_configured": bool(self.api_key), "model": self.model,
                "base_url": self.base_url, "api_style": self.api_style,
                "enable_thinking": self.enable_thinking}
