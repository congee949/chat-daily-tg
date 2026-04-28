import httpx
import pytest
from pytest_httpx import HTTPXMock
from chat_daily_tg.llm_client import LLMClient


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
        model="claude-sonnet-4-6",
        api_key="test-key",
        max_tokens=100,
    )
    text, usage = client.chat("say hi")
    assert text == "hello"
    assert usage["total_tokens"] == 15

    sent = httpx_mock.get_request()
    assert sent.headers["Authorization"] == "Bearer test-key"
    body = sent.read().decode()
    assert '"model":"claude-sonnet-4-6"' in body.replace(" ", "")
    assert '"max_tokens":100' in body.replace(" ", "")


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
