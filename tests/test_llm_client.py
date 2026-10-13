import json

import httpx
import pytest

from backend.core.llm_client import HTTPModelClient, ModelError


def client(handler, style="responses", attempts=2, **options):
    return HTTPModelClient(api_key="test-secret", model="test-model", api_style=style,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)), max_attempts=attempts, **options)


def test_chat_completion_parses_tool_calls_and_hides_internal_metadata():
    requests = []
    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"finish_reason": "tool_calls", "message": {
            "content": None, "tool_calls": [{"id": "call1", "type": "function", "function": {
                "name": "get_schedule", "arguments": '{"date":"2026-10-14"}'}}]}}]})
    model = client(handler, "chat_completions")
    r = model.complete([{"role": "user", "content": "课表", "_private": "internal"}], [], system="test", state={})
    assert r["tool_calls"][0]["id"] == "call1"
    assert "_private" not in requests[0]["messages"][1]


def test_responses_preserves_reasoning_and_matches_function_call_output():
    requests = []
    outputs = [
        {"type": "reasoning", "id": "reason1", "summary": [], "encrypted_content": "opaque"},
        {"type": "function_call", "id": "fc1", "call_id": "call1", "name": "get_schedule", "arguments": '{"date":"2026-10-14"}'},
    ]
    def handler(request):
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return httpx.Response(200, json={"status": "completed", "output": outputs})
        return httpx.Response(200, json={"status": "completed", "output": [
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "模拟无课"}]}]})
    model = client(handler)
    history = [{"role": "user", "content": "今天课表"}]
    r = model.complete(history, [], system="test", state={})
    history += [r, {"role": "tool", "tool_call_id": "call1", "content": '{"success":true}'}]
    assert model.complete(history, [], system="test", state={})["content"] == "模拟无课"
    assert requests[1]["input"][1:3] == outputs
    assert requests[1]["input"][3]["call_id"] == "call1"
    assert requests[1]["store"] is False


def test_transient_http_failure_retries_only_model_call():
    requests = []
    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(503)
        return httpx.Response(200, json={"status": "completed", "output": [
            {"type": "message", "content": [{"type": "output_text", "text": "hello"}]}]})
    assert client(handler).complete([], [], system="test", state={})["content"] == "hello"
    assert len(requests) == 2


@pytest.mark.parametrize("status,count", [(401, 1), (429, 2), (500, 2)])
def test_http_retry_limit_and_no_secret_leak(status, count):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(status, text="test-secret and private payload")
    with pytest.raises(ModelError) as error:
        client(handler).complete([], [], system="test", state={})
    assert len(requests) == count
    assert "test-secret" not in str(error.value)


def test_truncated_response_does_not_execute_partial_calls():
    model = client(lambda req: httpx.Response(200, json={"status": "incomplete", "output": []}))
    with pytest.raises(ModelError):
        model.complete([], [], system="test", state={})


def test_aliyun_thinking_parameter_and_reasoning_roundtrip():
    from backend.agent.planner import normalize_reply
    requests = []
    def handler(request):
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return httpx.Response(200, json={"choices": [{"finish_reason": "tool_calls", "message": {
                "content": "", "reasoning_content": "provider-only",
                "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "get_schedule", "arguments": '{}'}}]}}]})
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": "done"}}]})
    model = client(handler, "chat_completions", enable_thinking=False)
    first, _ = normalize_reply(model.complete([], [], system="test", state={}))
    final = model.complete([first, {"role": "tool", "tool_call_id": "c1", "content": "{}"}], [], system="test", state={})
    assert final["content"] == "done"
    assert requests[0]["enable_thinking"] is False
    assert requests[1]["messages"][1]["reasoning_content"] == "provider-only"
    assert "_chat_fields" not in requests[1]["messages"][1]


def test_probe_checks_two_request_function_roundtrip():
    from check_llm import probe
    requests = []
    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            import re
            nonce = re.search(r"nonce 为 ([a-f0-9]+)", body["messages"][-1]["content"])[1]
            return httpx.Response(200, json={"choices": [{"finish_reason": "tool_calls", "message": {
                "tool_calls": [{"id": "probe1", "type": "function", "function": {
                    "name": "campusflow_probe", "arguments": json.dumps({"nonce": nonce})}}]}}]})
        assert body["messages"][-1]["tool_call_id"] == "probe1"
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": "连接成功"}}]})
    assert probe(client(handler, "chat_completions"))["requests"] == 2
    assert len(requests) == 2


def test_probe_rejects_text_without_function_call():
    from check_llm import probe
    model = client(lambda request: httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": "我已经查好了"}}]}), "chat_completions")
    with pytest.raises(ModelError, match="未返回预期"):
        probe(model)
