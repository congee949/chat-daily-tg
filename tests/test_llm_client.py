import httpx
import pytest
from unittest.mock import patch
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


def test_chat_retries_200_with_malformed_body(httpx_mock: HTTPXMock):
    # 200 OK but no 'choices' (proxy error page / {"error":...}) — a soft failure
    # that must be retried, not crash the pipeline on the first call (finding #10).
    httpx_mock.add_response(
        url="http://127.0.0.1:8317/v1/chat/completions",
        method="POST",
        json={"error": "upstream rate limited"},
        is_reusable=True,
    )
    client = LLMClient(
        endpoint="http://127.0.0.1:8317/v1",
        model="m",
        api_key="k",
        max_tokens=100,
        retry_max_attempts=3,
        retry_backoff_seconds=[0],
    )
    with pytest.raises((KeyError, IndexError, ValueError)):
        client.chat("hi")
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


@pytest.mark.httpx_mock(can_send_already_matched_responses=True)
def test_chat_does_not_retry_non_retryable_400(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        url="http://127.0.0.1:8317/v1/chat/completions",
        method="POST", status_code=400,
    )
    client = LLMClient(
        endpoint="http://127.0.0.1:8317/v1", model="m", api_key="k",
        retry_max_attempts=3, retry_backoff_seconds=[0],
    )
    with pytest.raises(httpx.HTTPStatusError):
        client.chat("bad request")
    assert len(httpx_mock.get_requests()) == 1


def test_client_reuses_one_pool_and_closes_it():
    with patch("chat_daily_tg.llm_client.httpx.Client") as client_cls:
        http = client_cls.return_value.__enter__.return_value
        response = http.post.return_value
        response.json.return_value = {"choices": [{"message": {"content": "ok"}}]}
        response.headers = {}
        response.raise_for_status.return_value = None
        client = LLMClient(endpoint="http://x", model="m", api_key="secret")
        assert client.chat("one")[0] == "ok"
        assert client.chat("two")[0] == "ok"
        assert client_cls.call_count == 1
        client.close()
        client_cls.return_value.__exit__.assert_called_once()


@pytest.mark.httpx_mock(can_send_already_matched_responses=True)
def test_chat_retries_200_with_empty_content(httpx_mock: HTTPXMock):
    from chat_daily_tg.llm_client import LLMResponseError

    httpx_mock.add_response(
        url="http://127.0.0.1:8317/v1/chat/completions",
        method="POST",
        json={"choices": [{"message": {"role": "assistant", "content": ""}}]},
        is_reusable=True,
    )
    client = LLMClient(
        endpoint="http://127.0.0.1:8317/v1",
        model="m",
        api_key="k",
        max_tokens=100,
        retry_max_attempts=3,
        retry_backoff_seconds=[0],
        retry_jitter_seconds=0.0,
    )
    with pytest.raises(LLMResponseError):
        client.chat("hi")
    assert len(httpx_mock.get_requests()) == 3


@pytest.mark.httpx_mock(can_send_already_matched_responses=True)
def test_chat_does_not_retry_401_without_body(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        url="http://127.0.0.1:8317/v1/chat/completions",
        method="POST",
        status_code=401,
    )
    client = LLMClient(
        endpoint="http://127.0.0.1:8317/v1",
        model="m",
        api_key="k",
        retry_max_attempts=3,
        retry_backoff_seconds=[0],
    )
    with pytest.raises(httpx.HTTPStatusError) as ei:
        client.chat("nope")
    assert ei.value.response.status_code == 401
    assert len(httpx_mock.get_requests()) == 1



# --- Reliability matrix: table-driven retry / invalid body contracts ---

def _client(**kwargs):
    defaults = dict(
        endpoint="http://127.0.0.1:8317/v1",
        model="m",
        api_key="k",
        max_tokens=100,
        retry_max_attempts=3,
        retry_backoff_seconds=[0],
        retry_jitter_seconds=0.0,
    )
    defaults.update(kwargs)
    return LLMClient(**defaults)


@pytest.mark.parametrize(
    "body",
    [
        {"choices": [{"message": {"role": "assistant", "content": ""}}]},
        {"choices": [{"message": {"role": "assistant", "content": "   \n\t  "}}]},
        {"choices": [{"message": {"role": "assistant", "content": None}}]},
        {"choices": [{"message": {"role": "assistant"}}]},
        {"choices": [{}]},
        {"choices": []},
        {"error": {"message": "upstream", "code": "empty_non_stream_body"}},
        {"error": "plain"},
    ],
)
def test_chat_retries_invalid_or_empty_200_bodies(httpx_mock: HTTPXMock, body):
    httpx_mock.add_response(
        url="http://127.0.0.1:8317/v1/chat/completions",
        method="POST",
        json=body,
        is_reusable=True,
    )
    client = _client()
    with pytest.raises((ValueError, KeyError, IndexError, TypeError)):
        client.chat("hi")
    assert len(httpx_mock.get_requests()) == 3


def test_chat_retries_500_then_succeeds(httpx_mock: HTTPXMock):
    url = "http://127.0.0.1:8317/v1/chat/completions"
    httpx_mock.add_response(url=url, method="POST", status_code=500)
    httpx_mock.add_response(url=url, method="POST", status_code=500)
    httpx_mock.add_response(
        url=url,
        method="POST",
        json={"choices": [{"message": {"content": "recovered"}}], "usage": {}},
    )
    text, _ = _client().chat("hi")
    assert text == "recovered"
    assert len(httpx_mock.get_requests()) == 3


def test_chat_retries_429_then_succeeds(httpx_mock: HTTPXMock):
    url = "http://127.0.0.1:8317/v1/chat/completions"
    httpx_mock.add_response(
        url=url, method="POST", status_code=429, headers={"Retry-After": "0"}
    )
    httpx_mock.add_response(
        url=url,
        method="POST",
        json={"choices": [{"message": {"content": "ok-after-429"}}], "usage": {}},
    )
    text, _ = _client().chat("hi")
    assert text == "ok-after-429"
    assert len(httpx_mock.get_requests()) == 2


def test_chat_does_not_retry_401_with_json_body(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        url="http://127.0.0.1:8317/v1/chat/completions",
        method="POST",
        status_code=401,
        json={"error": "Missing or invalid Authorization header"},
    )
    with pytest.raises(httpx.HTTPStatusError) as ei:
        _client().chat("hi")
    assert ei.value.response.status_code == 401
    assert len(httpx_mock.get_requests()) == 1


def test_chat_retries_read_timeout_then_raises(httpx_mock: HTTPXMock):
    httpx_mock.add_exception(
        httpx.ReadTimeout("read timed out"),
        url="http://127.0.0.1:8317/v1/chat/completions",
        method="POST",
        is_reusable=True,
    )
    with pytest.raises(httpx.ReadTimeout):
        _client().chat("hi")
    assert len(httpx_mock.get_requests()) == 3


def test_retry_after_hour_is_capped(monkeypatch, httpx_mock: HTTPXMock):
    sleeps: list[float] = []
    monkeypatch.setattr(
        "chat_daily_tg.llm_client.time.sleep", lambda s: sleeps.append(float(s))
    )
    url = "http://127.0.0.1:8317/v1/chat/completions"
    httpx_mock.add_response(
        url=url, method="POST", status_code=429, headers={"Retry-After": "3600"}
    )
    httpx_mock.add_response(
        url=url, method="POST", status_code=429, headers={"Retry-After": "7200"}
    )
    httpx_mock.add_response(
        url=url, method="POST", status_code=429, headers={"Retry-After": "9999"}
    )
    with pytest.raises(httpx.HTTPStatusError):
        _client().chat("hi")
    assert len(httpx_mock.get_requests()) == 3
    assert sleeps == [60.0, 60.0]
    assert all(s <= 60.0 for s in sleeps)


def test_chat_success_logs_model_attempts_and_latency(httpx_mock: HTTPXMock, caplog):
    import logging

    url = "http://127.0.0.1:8317/v1/chat/completions"
    httpx_mock.add_response(url=url, method="POST", status_code=500)
    httpx_mock.add_response(
        url=url,
        method="POST",
        json={"choices": [{"message": {"content": "ok"}}], "usage": {}},
    )
    client = _client()
    with caplog.at_level(logging.INFO, logger="chat_daily_tg.llm_client"):
        text, _ = client.chat("hi")
    assert text == "ok"
    ok_lines = [r.getMessage() for r in caplog.records if "llm call ok" in r.getMessage()]
    assert len(ok_lines) == 1
    assert "model=m" in ok_lines[0]
    assert "attempts=2" in ok_lines[0]  # 500 then success → the log carries the attempt count
    assert "latency_ms=" in ok_lines[0]
    assert client.last_metrics is not None
    assert f"latency_ms={client.last_metrics.latency_ms}" in ok_lines[0]


def test_chat_failure_paths_emit_no_success_log(httpx_mock: HTTPXMock, caplog):
    import logging

    httpx_mock.add_response(
        url="http://127.0.0.1:8317/v1/chat/completions",
        method="POST",
        status_code=401,
    )
    with caplog.at_level(logging.INFO, logger="chat_daily_tg.llm_client"):
        with pytest.raises(httpx.HTTPStatusError):
            _client().chat("hi")
    assert not any("llm call ok" in r.getMessage() for r in caplog.records)


def test_exhausted_retries_do_not_touch_seen(httpx_mock: HTTPXMock, monkeypatch):
    """LLMClient has no seen side effects; exhausted raise must stay local."""
    calls: list[str] = []

    def _forbid(*_a, **_k):
        calls.append("seen")
        raise AssertionError("seen must not be advanced on LLM failure")

    monkeypatch.setattr("chat_daily_tg.llm_client.log.warning", lambda *a, **k: None)
    httpx_mock.add_response(
        url="http://127.0.0.1:8317/v1/chat/completions",
        method="POST",
        json={"choices": [{"message": {"content": ""}}]},
        is_reusable=True,
    )
    with pytest.raises(ValueError):
        _client().chat("hi")
    assert calls == []
    assert len(httpx_mock.get_requests()) == 3


@pytest.mark.parametrize(
    ("status", "body", "expected", "attempts"),
    [
        (400, {"error": {"code": "model_not_found", "message": "secret prompt and sk-super-secret-secret"}}, "model_not_found", 1),
        (504, {"error": {"code": "upstream_timeout", "message": "secret prompt and sk-super-secret-secret"}}, "upstream_timeout", 3),
        (504, "<html>secret prompt</html>", "unavailable", 3),
        (400, {"error": {"code": "secret prompt sk-danger", "message": "secret prompt"}}, "unavailable", 1),
    ],
)
def test_http_failure_reports_safe_code_only(httpx_mock, caplog, status, body, expected, attempts):
    import logging

    response = {"json": body} if isinstance(body, dict) else {"text": body}
    httpx_mock.add_response(
        url="http://127.0.0.1:8317/v1/chat/completions",
        method="POST", status_code=status, is_reusable=True, **response,
    )
    with caplog.at_level(logging.WARNING, logger="chat_daily_tg.llm_client"):
        with pytest.raises(httpx.HTTPStatusError) as raised:
            _client().chat("sensitive user prompt")
    assert raised.value.response.status_code == status
    assert len(httpx_mock.get_requests()) == attempts
    combined = str(raised.value) + "\n" + caplog.text
    assert f"HTTP {status} error_code={expected}" in combined
    assert "secret prompt" not in combined
    assert "sensitive user prompt" not in combined
    assert "sk-super-secret-secret" not in combined
    assert "8317" not in combined
