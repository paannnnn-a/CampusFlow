import json

import pytest

import check_llm
from backend.core.llm_config import LLMConfig


def settings(**changes):
    return LLMConfig(api_key="test-private-key", model="qwen-plus", **changes)


def test_config_hides_key_and_supports_dashscope_alias():
    config = LLMConfig.from_mapping({"DASHSCOPE_API_KEY": "test-private-key", "LLM_MODEL": "qwen-plus",
        "LLM_BASE_URL": "https://dashscope.aliyuncs.com/compatible-mode/v1/",
        "LLM_API_STYLE": "chat_completions", "LLM_ENABLE_THINKING": "false"})
    assert config.api_key == "test-private-key" and config.enable_thinking is False
    assert config.base_url.endswith("/v1")
    assert "test-private-key" not in repr(config)
    assert "test-private-key" not in json.dumps(config.safe_summary())


@pytest.mark.parametrize("url", [
    "not-a-url", "https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
    "https://api.example.com/v1/chat/completions", "https://api.example.com/v1/responses",
    "https://api.example.com/v1?api_key=private", "https://private:password@api.example.com/v1",
    "https://api.example.com /v1", "https://api.example.com/v1#private",
])
def test_malformed_endpoint_rejected_without_echoing_input(url):
    with pytest.raises(ValueError) as error:
        settings(base_url=url)
    assert url not in str(error.value)


@pytest.mark.parametrize("env", [
    {}, {"LLM_API_KEY": "key"}, {"LLM_MODEL": "qwen-plus"},
    {"LLM_API_KEY": "key", "LLM_MODEL": "qwen-plus", "LLM_ENABLE_THINKING": "not-a-boolean"},
])
def test_missing_or_invalid_configuration(env):
    with pytest.raises(ValueError):
        LLMConfig.from_mapping(env)


def test_provider_thinking_flag_is_not_sent_to_responses():
    with pytest.raises(ValueError, match="只用于"):
        settings(enable_thinking=False)


def test_local_check_never_constructs_http_client(monkeypatch, capsys):
    monkeypatch.setattr(check_llm, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("LLM_API_KEY", "test-private-key")
    monkeypatch.setenv("LLM_MODEL", "qwen-plus")
    monkeypatch.setenv("LLM_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
    monkeypatch.setenv("LLM_API_STYLE", "chat_completions")
    monkeypatch.setenv("LLM_ENABLE_THINKING", "false")
    def forbidden():
        raise AssertionError("offline check must not construct network client")
    monkeypatch.setattr(check_llm.HTTPModelClient, "from_env", forbidden)
    assert check_llm.main([]) == 0
    output = capsys.readouterr().out
    assert "未联网" in output and "test-private-key" not in output
