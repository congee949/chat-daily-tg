"""Best-effort client for the Hermes incident controller.

The bridge is opt-in: no request is made unless both the controller endpoint
and a protected bearer-token file are configured.  Alert delivery must never
depend on this integration, so the public helpers contain all network and
configuration failures and return a small success signal instead.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import stat
import uuid
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

import httpx

from chat_daily_tg.logging_setup import redact


log = logging.getLogger(__name__)

ENDPOINT_ENV = "CHAT_DAILY_HERMES_INCIDENT_ENDPOINT"
TOKEN_FILE_ENV = "CHAT_DAILY_HERMES_INCIDENT_TOKEN_FILE"
SOURCE = "chat-daily-tg"
SERVICE = "chat-daily-tg"
TARGET_ID = "chat-daily-tg"
REQUEST_TIMEOUT_SECONDS = 2.0
MAX_TITLE_BYTES = 2_000
MAX_MESSAGE_BYTES = 24_000
MAX_METADATA_BYTES = 8_000
MAX_TOKEN_BYTES = 4_096


def report_warning(
    title: str,
    message: str,
    *,
    severity: str = "warning",
    source_event_id: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> str | None:
    """Report one warning occurrence and return its accepted source event ID.

    Callers that may retry the *same* source event should create an ID once and
    pass it on every attempt.  Calls without an ID deliberately receive a new
    UUID: repeated pipeline failures are separate occurrences used by Hermes'
    minimum-occurrence gate, rather than retries of one HTTP event.
    """
    event_id = _source_event_id(source_event_id)
    level = str(severity or "warning").casefold()
    if level not in {"warning", "error", "critical"}:
        level = "warning"
    payload = _base_payload(
        kind=level,
        severity=level,
        title=title,
        message=message,
        source_event_id=event_id,
        metadata=metadata,
    )
    return event_id if _post("/v1/incidents", payload) else None


def report_recovery(
    source_event_id: str,
    *,
    title: str = "chat-daily-tg recovered",
    message: str = "producer health check recovered",
    metadata: Mapping[str, Any] | None = None,
) -> bool:
    """Send an authoritative recovery for a previously reported warning.

    Wrappers should retain the warning's ``source_event_id`` and reuse it here.
    Keeping source/service/target fields identical also lets the controller
    resolve the matching active incident without accepting arbitrary scope.
    """
    event_id = _source_event_id(source_event_id, generate=False)
    if not event_id:
        return False
    payload = _base_payload(
        kind="recovery",
        severity="info",
        title=title,
        message=message,
        source_event_id=event_id,
        metadata=metadata,
    )
    return _post("/v1/recoveries", payload)


def _base_payload(
    *,
    kind: str,
    severity: str,
    title: str,
    message: str,
    source_event_id: str,
    metadata: Mapping[str, Any] | None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "source": SOURCE,
        "service": SERVICE,
        "host": _limited(redact(socket.gethostname()), 256),
        "severity": severity,
        "kind": kind,
        "title": _limited(redact(str(title or "")), MAX_TITLE_BYTES),
        "message": _limited(redact(str(message or "")), MAX_MESSAGE_BYTES),
        "target_id": TARGET_ID,
        "source_event_id": source_event_id,
    }
    clean_metadata = _clean_metadata(metadata)
    if clean_metadata is not None:
        payload["metadata"] = clean_metadata
    return payload


def _post(path: str, payload: dict[str, Any]) -> bool:
    """POST one event; disabled/misconfigured/unavailable all fail closed."""
    endpoint = os.environ.get(ENDPOINT_ENV, "").strip()
    token_path = os.environ.get(TOKEN_FILE_ENV, "").strip()
    if not endpoint:
        return False
    try:
        base_url = _base_url(endpoint)
        if not token_path:
            raise PermissionError(f"{TOKEN_FILE_ENV} is required")
        token = _read_token(Path(token_path))
        safe_payload = _remove_secret(payload, token)
        with httpx.Client(
            timeout=REQUEST_TIMEOUT_SECONDS,
            trust_env=False,
            follow_redirects=False,
        ) as client:
            response = client.post(
                base_url + path,
                headers={"Authorization": f"Bearer {token}"},
                json=safe_payload,
            )
        if response.status_code != 202:
            raise RuntimeError(f"unexpected incident response status {response.status_code}")
        value = response.json()
        if not isinstance(value, dict) or value.get("accepted") is not True:
            raise RuntimeError("incident controller did not accept the event")
        return True
    except Exception as exc:  # incident reporting must never break the producer
        log.warning("Hermes incident report failed: %s", redact(str(exc)))
        return False


def _base_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"{ENDPOINT_ENV} must be an http(s) base URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError(f"{ENDPOINT_ENV} must not contain credentials, query, or fragment")
    if parsed.path not in {"", "/"}:
        raise ValueError(f"{ENDPOINT_ENV} must be a base URL without an API path")
    return value.rstrip("/")


def _read_token(path: Path) -> str:
    if not path.is_absolute():
        raise PermissionError(f"{TOKEN_FILE_ENV} must be an absolute path")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
            raise PermissionError("incident token must be a mode-0600 regular file")
        if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
            raise PermissionError("incident token file must be owned by the current user")
        raw = os.read(descriptor, MAX_TOKEN_BYTES + 1)
    finally:
        os.close(descriptor)
    if len(raw) > MAX_TOKEN_BYTES:
        raise PermissionError("incident token file is too large")
    try:
        token = raw.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise PermissionError("incident token file is not UTF-8") from exc
    if len(token.encode("utf-8")) < 32 or any(char.isspace() for char in token):
        raise PermissionError("incident token is invalid")
    return token


def _source_event_id(value: str | None, *, generate: bool = True) -> str:
    cleaned = _limited(redact(str(value or "").strip()), 256)
    if cleaned:
        return cleaned
    return f"{SOURCE}:{uuid.uuid4().hex}" if generate else ""


def _clean_metadata(value: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    try:
        encoded = json.dumps(dict(value), ensure_ascii=False, sort_keys=True, default=str)
        cleaned = _limited(redact(encoded), MAX_METADATA_BYTES)
        decoded = json.loads(cleaned)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {"invalid": True}
    return decoded if isinstance(decoded, dict) else {"invalid": True}


def _remove_secret(payload: dict[str, Any], secret: str) -> dict[str, Any]:
    """Defense in depth if the controller bearer token appears in alert text."""
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    if secret:
        encoded = encoded.replace(secret, "<REDACTED_SECRET>")
    value = json.loads(encoded)
    assert isinstance(value, dict)
    return value


def _limited(value: str, limit: int) -> str:
    encoded = value.encode("utf-8", errors="replace")
    if len(encoded) <= limit:
        return value
    suffix = "\n…[truncated]"
    keep = max(0, limit - len(suffix.encode("utf-8")))
    return encoded[:keep].decode("utf-8", errors="ignore") + suffix


__all__ = [
    "ENDPOINT_ENV",
    "TOKEN_FILE_ENV",
    "report_recovery",
    "report_warning",
]
