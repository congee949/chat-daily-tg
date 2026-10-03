"""Hermetic contract between chat-daily LLMClient and QwenProxy error shapes.

No network. Fixture lists stable codes / retryability; client behavior is
asserted against the same rules so the two repos cannot drift silently.
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from pytest_httpx import HTTPXMock

from chat_daily_tg.llm_client import (
    LLMClient,
    LLMResponseError,
    _RETRYABLE_STATUSES,
    _RETRY_AFTER_MIN_CAP_SECONDS,
)

FIXTURE = Path(__file__).parent / "fixtures" / "qwenproxy_error_codes.json"


def _load_contract() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_fixture_retryable_statuses_match_client():
    contract = _load_contract()
    assert set(contract["retryable_http_statuses"]) == set(_RETRYABLE_STATUSES)
    assert 401 in contract["non_retryable_http_statuses"]
    assert 401 not in _RETRYABLE_STATUSES
    assert contract["retry_after_client_cap_seconds"] == int(_RETRY_AFTER_MIN_CAP_SECONDS)


@pytest.mark.parametrize(
    "entry",
    _load_contract()["codes"],
    ids=lambda e: e["code"],
)
def test_contract_entry_retryability_matches_client_rules(entry: dict):
    status = int(entry["http_status"])
    retryable = bool(entry["retryable"])
    if status == 200:
        # Soft failures on 200 are LLMResponseError, always retryable in client.
        assert retryable is True
        return
    assert (status in _RETRYABLE_STATUSES) is retryable


@pytest.mark.httpx_mock(can_send_already_matched_responses=True)
def test_401_invalid_api_key_never_retried(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        url="http://127.0.0.1:3000/v1/chat/completions",
        method="POST",
        status_code=401,
        json={"error": {"code": "invalid_api_key", "message": "Invalid Authorization"}},
    )
    client = LLMClient(
        endpoint="http://127.0.0.1:3000/v1",
        model="qwen3.7-plus-no-thinking",
        api_key="bad-key",
        retry_max_attempts=3,
        retry_backoff_seconds=[0],
        retry_jitter_seconds=0.0,
    )
    with pytest.raises(httpx.HTTPStatusError) as ei:
        client.chat("ping")
    assert ei.value.response.status_code == 401
    assert len(httpx_mock.get_requests()) == 1


@pytest.mark.httpx_mock(can_send_already_matched_responses=True)
def test_200_empty_content_is_retryable_llm_response_error(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        url="http://127.0.0.1:3000/v1/chat/completions",
        method="POST",
        json={
            "choices": [{"message": {"role": "assistant", "content": ""}}],
            "usage": {},
        },
        is_reusable=True,
    )
    client = LLMClient(
        endpoint="http://127.0.0.1:3000/v1",
        model="qwen3.7-plus-no-thinking",
        api_key="k",
        retry_max_attempts=3,
        retry_backoff_seconds=[0],
        retry_jitter_seconds=0.0,
    )
    with pytest.raises(LLMResponseError) as ei:
        client.chat("ping")
    assert ei.value.error_code == "empty_content"
    assert len(httpx_mock.get_requests()) == 3


@pytest.mark.httpx_mock(can_send_already_matched_responses=True)
def test_error_code_from_200_error_payload_attached(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        url="http://127.0.0.1:3000/v1/chat/completions",
        method="POST",
        json={"error": {"code": "empty_non_stream_body", "message": "empty body"}},
        is_reusable=True,
    )
    client = LLMClient(
        endpoint="http://127.0.0.1:3000/v1",
        model="m",
        api_key="k",
        retry_max_attempts=2,
        retry_backoff_seconds=[0],
        retry_jitter_seconds=0.0,
    )
    with pytest.raises(LLMResponseError) as ei:
        client.chat("ping")
    assert ei.value.error_code == "empty_non_stream_body"
    assert len(httpx_mock.get_requests()) == 2
