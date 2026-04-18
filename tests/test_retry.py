from pytest_httpx import HTTPXMock
import pytest
from wx_daily_tg.llm_client import LLMClient


def test_llm_client_retries_on_500(httpx_mock: HTTPXMock):
    # Two 500s then a success
    httpx_mock.add_response(url="http://127.0.0.1:8317/v1/chat/completions",
                             method="POST", status_code=500)
    httpx_mock.add_response(url="http://127.0.0.1:8317/v1/chat/completions",
                             method="POST", status_code=500)
    httpx_mock.add_response(url="http://127.0.0.1:8317/v1/chat/completions",
                             method="POST",
                             json={"choices":[{"message":{"content":"ok"}}],"usage":{}})
    c = LLMClient(
        endpoint="http://127.0.0.1:8317/v1",
        model="m", api_key="k", max_tokens=10,
        retry_max_attempts=3, retry_backoff_seconds=[0, 0, 0],
    )
    text, _ = c.chat("hi")
    assert text == "ok"
    assert len(httpx_mock.get_requests()) == 3
