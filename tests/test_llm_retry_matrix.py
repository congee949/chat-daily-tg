"""Table-driven retry matrix for LLMClient (non-stream OpenAI JSON path)."""
from __future__ import annotations

import httpx
import pytest
from pytest_httpx import HTTPXMock
from unittest.mock import patch

from chat_daily_tg.llm_client import LLMClient, LLMResponseError, _RETRY_AFTER_MIN_CAP_SECONDS


URL = "http://127.0.0.1:3000/v1/chat/completions"
ENDPOINT = "http://127.0.0.1:3000/v1"


def _client(**kwargs) -> LLMClient:
    defaults = dict(
        endpoint=ENDPOINT,
        model="qwen3.7-plus-no-thinking",
        api_key="test-key",
        max_tokens=64,
        retry_max_attempts=3,
        retry_backoff_seconds=[0],
        retry_jitter_seconds=0.0,
    )
    defaults.update(kwargs)
    return LLMClient(**defaults)


def _ok_body(content: str = "ok") -> dict:
    return {
        "choices": [{"message": {"role": "assistant", "content": content}}],
        "usage": {"total_tokens": 1},
    }


@pytest.mark.httpx_mock(can_send_already_matched_responses=True)
@pytest.mark.parametrize(
    "body,expect_code",
    [
        (
            {"choices": [{"message": {"role": "assistant", "content": ""}}]},
            "empty_content",
        ),
        (
            {"choices": [{"message": {"role": "assistant", "content": "   \n\t  "}}]},
            "empty_content",
        ),
        (
            {"choices": [{"message": {"role": "assistant", "content": None}}]},
            None,
        ),
        (
            {"choices": [{"message": {"role": "assistant"}}]},
            None,
        ),
        (
            {"error": {"code": "malformed_response", "message": "no choices"}},
            "malformed_response",
        ),
        (
            {"choices": []},
            None,
        ),
        (
            {"choices": [{"message": "not-a-dict"}]},
            None,
        ),
    ],
    ids=[
        "empty_string",
        "whitespace_only",
        "null_content",
        "missing_content",
        "error_payload_no_choices",
        "empty_choices",
        "invalid_choice_message",
    ],
)
def test_nonstream_invalid_200_retried_then_raises(
    httpx_mock: HTTPXMock, body: dict, expect_code: str | None
):
    httpx_mock.add_response(url=URL, method="POST", json=body, is_reusable=True)
    client = _client(retry_max_attempts=3)
    with pytest.raises(LLMResponseError) as ei:
        client.chat("hi")
    assert len(httpx_mock.get_requests()) == 3
    if expect_code is not None:
        assert ei.value.error_code == expect_code


@pytest.mark.httpx_mock(can_send_already_matched_responses=True)
def test_500_then_success(httpx_mock: HTTPXMock):
    httpx_mock.add_response(url=URL, method="POST", status_code=500)
    httpx_mock.add_response(url=URL, method="POST", json=_ok_body("recovered"))
    client = _client()
    text, _ = client.chat("hi")
    assert text == "recovered"
    assert len(httpx_mock.get_requests()) == 2
    assert client.last_metrics is not None
    assert client.last_metrics.attempts == 2


@pytest.mark.httpx_mock(can_send_already_matched_responses=True)
def test_429_then_success(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        url=URL,
        method="POST",
        status_code=429,
        json={"error": {"code": "RateLimited", "message": "slow down"}},
        headers={"Retry-After": "1"},
    )
    httpx_mock.add_response(url=URL, method="POST", json=_ok_body("after-429"))
    client = _client()
    with patch("chat_daily_tg.llm_client.time.sleep") as sleep:
        text, _ = client.chat("hi")
    assert text == "after-429"
    assert len(httpx_mock.get_requests()) == 2
    assert sleep.call_count == 1


@pytest.mark.httpx_mock(can_send_already_matched_responses=True)
def test_read_timeout_exhausted(httpx_mock: HTTPXMock):
    httpx_mock.add_exception(
        httpx.ReadTimeout("The read operation timed out"),
        url=URL,
        method="POST",
        is_reusable=True,
    )
    client = _client(retry_max_attempts=3)
    with pytest.raises(httpx.ReadTimeout):
        client.chat("hi")
    assert len(httpx_mock.get_requests()) == 3


@pytest.mark.httpx_mock(can_send_already_matched_responses=True)
def test_connection_interrupt_exhausted(httpx_mock: HTTPXMock):
    httpx_mock.add_exception(
        httpx.RemoteProtocolError("Server disconnected without sending a response."),
        url=URL,
        method="POST",
        is_reusable=True,
    )
    client = _client(retry_max_attempts=3)
    with pytest.raises(httpx.RemoteProtocolError):
        client.chat("hi")
    assert len(httpx_mock.get_requests()) == 3


@pytest.mark.httpx_mock(can_send_already_matched_responses=True)
def test_401_never_retried(httpx_mock: HTTPXMock):
    httpx_mock.add_response(url=URL, method="POST", status_code=401)
    client = _client(retry_max_attempts=5)
    with pytest.raises(httpx.HTTPStatusError) as ei:
        client.chat("hi")
    assert ei.value.response.status_code == 401
    assert len(httpx_mock.get_requests()) == 1


@pytest.mark.httpx_mock(can_send_already_matched_responses=True)
def test_retry_after_3600_capped_and_attempt_budget_held(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        url=URL,
        method="POST",
        status_code=429,
        headers={"Retry-After": "3600"},
        json={"error": {"code": "RateLimited"}},
        is_reusable=True,
    )
    client = _client(
        retry_max_attempts=3,
        retry_backoff_seconds=[0],
        retry_jitter_seconds=0.0,
    )
    sleeps: list[float] = []
    with patch("chat_daily_tg.llm_client.time.sleep", side_effect=lambda s: sleeps.append(s)):
        with pytest.raises(httpx.HTTPStatusError):
            client.chat("hi")
    assert len(httpx_mock.get_requests()) == 3
    # Two sleeps between three attempts; each capped well below 3600s.
    assert len(sleeps) == 2
    assert all(s == _RETRY_AFTER_MIN_CAP_SECONDS for s in sleeps)
    assert all(s <= 60.0 for s in sleeps)


@pytest.mark.httpx_mock(can_send_already_matched_responses=True)
def test_empty_content_then_success_is_retried(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        url=URL,
        method="POST",
        json=_ok_body(""),
    )
    httpx_mock.add_response(url=URL, method="POST", json=_ok_body("filled"))
    client = _client()
    text, _ = client.chat("hi")
    assert text == "filled"
    assert len(httpx_mock.get_requests()) == 2


@pytest.mark.httpx_mock(can_send_already_matched_responses=True)
def test_retry_exhausted_only_raises_no_side_channel_state(httpx_mock: HTTPXMock):
    """LLMClient has no seen API; exhaustion must only raise (no silent success)."""
    httpx_mock.add_response(
        url=URL,
        method="POST",
        json=_ok_body("  "),
        is_reusable=True,
    )
    client = _client(retry_max_attempts=2)
    with pytest.raises(LLMResponseError):
        client.chat("hi")
    assert client.last_metrics is None
    assert len(httpx_mock.get_requests()) == 2
