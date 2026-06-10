import httpx
import pytest
from pytest_httpx import HTTPXMock
from chat_daily_tg.llm_client import LLMClient

# Proxy env vars are cleared globally by the autouse fixture in tests/conftest.py.


def test_chat_completion_posts_correct_shape(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        url="http://127.0.0.1:8317/v1/chat/completions",
        method="POST",
        json={
            "choices": [{"message": {"role": "assistant", "content": "hello"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        },
    )
    client = LLMClient(
        endpoint="http://127.0.0.1:8317/v1",
        model="test-summary-model",
        api_key="test-key",
        max_tokens=100,
    )
    text, usage = client.chat("say hi")
    assert text == "hello"
    assert usage["total_tokens"] == 15

    sent = httpx_mock.get_request()
    assert sent.headers["Authorization"] == "Bearer test-key"
    body = sent.read().decode()
    assert '"model":"test-summary-model"' in body.replace(" ", "")
    assert '"max_tokens":100' in body.replace(" ", "")


def test_chat_retries_remote_protocol_error_then_raises(httpx_mock: HTTPXMock):
    # Regression for 2026-06-10: mid-request disconnect (e.g. proxy tunnel torn
    # down during system sleep) raised RemoteProtocolError, which the retry loop
    # did not catch — one network failure killed the whole pipeline.
    httpx_mock.add_exception(
        httpx.RemoteProtocolError("Server disconnected without sending a response."),
        url="http://127.0.0.1:8317/v1/chat/completions",
        method="POST",
        is_reusable=True,
    )
    client = LLMClient(
        endpoint="http://127.0.0.1:8317/v1",
        model="test-summary-model",
        api_key="test-key",
        max_tokens=100,
        retry_max_attempts=3,
        retry_backoff_seconds=[0],
    )
    with pytest.raises(httpx.RemoteProtocolError):
        client.chat("say hi")
    assert len(httpx_mock.get_requests()) == 3


def test_chat_completion_merges_provider_extra_body(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        url="https://api.deepseek.com/chat/completions",
        method="POST",
        json={
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
            "usage": {"total_tokens": 3},
        },
    )
    client = LLMClient(
        endpoint="https://api.deepseek.com",
        model="deepseek-v4-pro",
        api_key="test-key",
        max_tokens=100,
        extra_body={
            "thinking": {"type": "enabled"},
            "reasoning_effort": "max",
        },
    )
    text, _ = client.chat("say hi")
    assert text == "ok"

    sent = httpx_mock.get_request()
    body = sent.read().decode()
    compact = body.replace(" ", "")
    assert '"model":"deepseek-v4-pro"' in compact
    assert '"thinking":{"type":"enabled"}' in compact
    assert '"reasoning_effort":"max"' in compact
