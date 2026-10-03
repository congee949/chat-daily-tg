from __future__ import annotations

import json

import httpx

from chat_daily_tg import incident_client


TOKEN = "incident-test-token-" + "x" * 32
BASE_URL = "http://100.64.0.8:18766"


def _enable(monkeypatch, tmp_path):
    token_file = tmp_path / "incident-token"
    token_file.write_text(TOKEN + "\n", encoding="utf-8")
    token_file.chmod(0o600)
    monkeypatch.setenv(incident_client.ENDPOINT_ENV, BASE_URL)
    monkeypatch.setenv(incident_client.TOKEN_FILE_ENV, str(token_file))
    return token_file


def test_warning_bridge_is_disabled_without_endpoint(monkeypatch, httpx_mock):
    monkeypatch.delenv(incident_client.ENDPOINT_ENV, raising=False)
    monkeypatch.delenv(incident_client.TOKEN_FILE_ENV, raising=False)

    assert incident_client.report_warning("failed", "details") is None
    assert httpx_mock.get_requests() == []


def test_report_warning_posts_authenticated_structured_event(
    monkeypatch, tmp_path, httpx_mock
):
    _enable(monkeypatch, tmp_path)
    httpx_mock.add_response(
        method="POST",
        url=BASE_URL + "/v1/incidents",
        status_code=202,
        json={"accepted": True, "incident": {"incident_id": "inc_1"}},
    )

    accepted = incident_client.report_warning(
        "pipeline failed",
        "worker timed out",
        severity="error",
        source_event_id="daily:2026-08-25:channels",
        metadata={"stage": "channels"},
    )

    assert accepted == "daily:2026-08-25:channels"
    request = httpx_mock.get_request()
    assert request is not None
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"
    body = json.loads(request.content)
    assert body == {
        "source": "chat-daily-tg",
        "service": "chat-daily-tg",
        "host": body["host"],
        "severity": "error",
        "kind": "error",
        "title": "pipeline failed",
        "message": "worker timed out",
        "target_id": "chat-daily-tg",
        "source_event_id": "daily:2026-08-25:channels",
        "metadata": {"stage": "channels"},
    }


def test_explicit_source_event_id_is_reused_for_retry(
    monkeypatch, tmp_path, httpx_mock
):
    _enable(monkeypatch, tmp_path)
    for duplicate in (False, True):
        httpx_mock.add_response(
            method="POST",
            url=BASE_URL + "/v1/incidents",
            status_code=202,
            json={"accepted": True, "incident": {"duplicate_event": duplicate}},
        )

    assert incident_client.report_warning("same", "event", source_event_id="evt-42") == "evt-42"
    assert incident_client.report_warning("same", "event", source_event_id="evt-42") == "evt-42"

    requests = httpx_mock.get_requests()
    assert [json.loads(request.content)["source_event_id"] for request in requests] == [
        "evt-42",
        "evt-42",
    ]


def test_unkeyed_warning_occurrences_get_distinct_ids(monkeypatch, tmp_path, httpx_mock):
    _enable(monkeypatch, tmp_path)
    for _ in range(2):
        httpx_mock.add_response(
            method="POST",
            url=BASE_URL + "/v1/incidents",
            status_code=202,
            json={"accepted": True, "incident": {}},
        )

    first = incident_client.report_warning("same", "failure")
    second = incident_client.report_warning("same", "failure")

    assert first and second and first != second


def test_report_recovery_reuses_warning_scope_and_event_id(
    monkeypatch, tmp_path, httpx_mock
):
    _enable(monkeypatch, tmp_path)
    httpx_mock.add_response(
        method="POST",
        url=BASE_URL + "/v1/recoveries",
        status_code=202,
        json={"accepted": True, "incident": {"state": "RESOLVED"}},
    )

    assert incident_client.report_recovery(
        "evt-42", metadata={"health_check": "ok"}
    ) is True
    body = json.loads(httpx_mock.get_request().content)
    assert body["kind"] == "recovery"
    assert body["severity"] == "info"
    assert body["source_event_id"] == "evt-42"
    assert body["source"] == "chat-daily-tg"
    assert body["service"] == "chat-daily-tg"
    assert body["target_id"] == "chat-daily-tg"


def test_body_is_redacted_and_controller_token_never_leaves_in_json(
    monkeypatch, tmp_path, httpx_mock
):
    _enable(monkeypatch, tmp_path)
    httpx_mock.add_response(
        method="POST",
        url=BASE_URL + "/v1/incidents",
        status_code=202,
        json={"accepted": True, "incident": {}},
    )
    telegram_token = "123456789:AAr" + "a" * 35

    incident_client.report_warning(
        "Authorization: Bearer secret-value-123456789",
        f"tg={telegram_token}; controller={TOKEN}",
        source_event_id="evt-secret",
    )

    request = httpx_mock.get_request()
    raw_body = request.content.decode("utf-8")
    assert telegram_token not in raw_body
    assert "secret-value-123456789" not in raw_body
    assert TOKEN not in raw_body
    assert "REDACTED" in raw_body
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"


def test_insecure_token_file_fails_closed_without_request(
    monkeypatch, tmp_path, httpx_mock
):
    token_file = _enable(monkeypatch, tmp_path)
    token_file.chmod(0o644)

    assert incident_client.report_warning("failed", "details") is None
    assert httpx_mock.get_requests() == []


def test_transport_failure_is_contained(monkeypatch, tmp_path, httpx_mock):
    _enable(monkeypatch, tmp_path)
    httpx_mock.add_exception(httpx.ConnectTimeout("offline"))

    assert incident_client.report_warning("failed", "details") is None


def test_client_uses_short_timeout_and_ignores_proxy_environment(
    monkeypatch, tmp_path
):
    _enable(monkeypatch, tmp_path)
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:9999")
    observed = {}

    class FakeResponse:
        status_code = 202

        @staticmethod
        def json():
            return {"accepted": True, "incident": {}}

    class FakeClient:
        def __init__(self, **kwargs):
            observed.update(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def post(self, url, **kwargs):
            observed["url"] = url
            return FakeResponse()

    monkeypatch.setattr(incident_client.httpx, "Client", FakeClient)

    assert incident_client.report_warning("failed", "details")
    assert observed["timeout"] == 2.0
    assert observed["trust_env"] is False
    assert observed["follow_redirects"] is False
