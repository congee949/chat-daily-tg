from __future__ import annotations

from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
import logging
import random
import re
import time
from typing import Any

import httpx

log = logging.getLogger(__name__)

_RESERVED_BODY_KEYS = frozenset({"model", "messages", "max_tokens"})
_RETRYABLE_STATUSES = frozenset({408, 409, 425, 429, 500, 501, 502, 503, 504})
_SAFE_ERROR_CODE = re.compile(r"[A-Za-z0-9_.-]{1,64}\Z")
# Cap server Retry-After so multi-hour proxy cooldowns cannot amplify a single
# client call into hours of sleep. Bounded attempts still exhaust quickly.
_RETRY_AFTER_MIN_CAP_SECONDS = 60.0
_MAX_RETRY_AFTER_SECONDS = _RETRY_AFTER_MIN_CAP_SECONDS  # alias


class LLMResponseError(ValueError):
    """A successful HTTP response that is not a usable chat-completions response."""

    def __init__(self, message: str, *, code: str | None = None, error_code: str | None = None) -> None:
        super().__init__(message)
        # ``code`` and ``error_code`` are aliases (matrix + contract tests use both).
        self.code = code if code is not None else error_code
        self.error_code = self.code


@dataclass(frozen=True)
class LLMCallMetrics:
    """Metrics from the most recent call, available without changing ``chat``'s API."""

    model: str
    attempts: int
    latency_ms: int
    input_chars: int
    usage: dict[str, Any]
    request_id: str | None


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    try:
        delay = max(0.0, float(raw))
    except ValueError:
        try:
            target = parsedate_to_datetime(raw)
            if target.tzinfo is None:
                return None
            delay = max(0.0, target.timestamp() - time.time())
        except (TypeError, ValueError, IndexError, OverflowError):
            return None
    return min(delay, _RETRY_AFTER_MIN_CAP_SECONDS)


def _is_retryable_http_error(error: httpx.HTTPStatusError) -> bool:
    # 401/403/400 must never be retried — auth and client errors are terminal.
    return error.response.status_code in _RETRYABLE_STATUSES


def _proxy_error_code(data: dict[str, Any]) -> str | None:
    """Extract machine-readable error.code from OpenAI-ish or proxy error bodies."""
    err = data.get("error")
    if isinstance(err, dict):
        code = err.get("code")
        if isinstance(code, str) and _SAFE_ERROR_CODE.fullmatch(code):
            return code
    code = data.get("code")
    if isinstance(code, str) and _SAFE_ERROR_CODE.fullmatch(code):
        return code
    return None


@dataclass
class LLMClient:
    """OpenAI-compatible synchronous client with a reusable connection pool.

    The client keeps one ``httpx.Client`` for all calls and retries in its own
    lifetime.  Callers that own a longer-lived registry may inject ``client``;
    only internally created clients are closed by :meth:`close`.
    """

    endpoint: str
    model: str
    api_key: str = field(repr=False)
    max_tokens: int = 16000
    timeout: float = 300.0
    retry_max_attempts: int = 3
    retry_backoff_seconds: list[float] = field(default_factory=lambda: [5, 15, 60])
    retry_jitter_seconds: float = 1.0
    extra_body: dict[str, Any] = field(default_factory=dict)
    client: httpx.Client | None = field(default=None, repr=False)
    last_metrics: LLMCallMetrics | None = field(default=None, init=False)
    _owned_client_context: httpx.Client | None = field(default=None, init=False, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        conflict = _RESERVED_BODY_KEYS.intersection(self.extra_body)
        if conflict:
            names = ", ".join(sorted(conflict))
            raise ValueError(f"extra_body cannot override request field(s): {names}")

    def _http_client(self) -> httpx.Client:
        if self._closed:
            raise RuntimeError("LLMClient is closed")
        if self.client is None:
            # Enter once and retain the pool.  Calling __enter__ also preserves
            # the standard httpx lifecycle for injected test transports.
            self._owned_client_context = httpx.Client(timeout=self.timeout)
            self.client = self._owned_client_context.__enter__()
        return self.client

    def close(self) -> None:
        """Release the internally-created connection pool (safe to call repeatedly)."""
        if self._closed:
            return
        self._closed = True
        if self._owned_client_context is not None:
            self._owned_client_context.__exit__(None, None, None)
        self._owned_client_context = None
        self.client = None

    def __enter__(self) -> "LLMClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def __del__(self) -> None:  # pragma: no cover - safety net for legacy callers
        try:
            self.close()
        except Exception:
            pass

    def _sleep_before_retry(self, *, attempt: int, response: httpx.Response | None = None) -> None:
        server_delay = _retry_after(response) if response is not None else None
        if server_delay is not None:
            delay = server_delay
        else:
            index = min(attempt - 1, len(self.retry_backoff_seconds) - 1)
            base = self.retry_backoff_seconds[index] if self.retry_backoff_seconds else 0.0
            delay = base + random.uniform(0.0, max(0.0, self.retry_jitter_seconds))
        if delay > 0:
            time.sleep(delay)

    @staticmethod
    def _parse_response(response: httpx.Response) -> tuple[str, dict[str, Any]]:
        try:
            data = response.json()
        except ValueError as exc:
            raise LLMResponseError(
                "chat-completions response was not valid JSON",
                code="invalid_json",
            ) from exc
        if not isinstance(data, dict):
            raise LLMResponseError(
                "chat-completions response must be a JSON object",
                code="invalid_json",
            )
        # Proxy may return HTTP 200 with {"error":{"code":...}} — treat as invalid.
        proxy_code = _proxy_error_code(data)
        if proxy_code and "choices" not in data:
            raise LLMResponseError(
                f"chat-completions error body code={proxy_code}",
                code=proxy_code,
            )
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise LLMResponseError(
                "chat-completions response has no choices",
                code=proxy_code or "malformed_choices",
            )
        first = choices[0]
        if not isinstance(first, dict) or not isinstance(first.get("message"), dict):
            raise LLMResponseError(
                "chat-completions response has an invalid choice",
                code="malformed_choices",
            )
        content = first["message"].get("content")
        if not isinstance(content, str):
            raise LLMResponseError(
                "chat-completions response has no text content",
                code="empty_content",
            )
        if not content.strip():
            raise LLMResponseError(
                "chat-completions response has empty text content",
                code="empty_content",
            )
        usage = data.get("usage", {})
        return content, usage if isinstance(usage, dict) else {}

    def chat(self, prompt: str, system: str | None = None) -> tuple[str, dict[str, Any]]:
        messages = []
        if system is not None:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        payload = {
            "model": self.model,
            "messages": messages,
            "max_tokens": self.max_tokens,
            **self.extra_body,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        last_exc: Exception | None = None
        started = time.monotonic()
        for attempt in range(1, self.retry_max_attempts + 1):
            response: httpx.Response | None = None
            try:
                response = self._http_client().post(
                    f"{self.endpoint.rstrip('/')}/chat/completions",
                    json=payload,
                    headers=headers,
                )
                response.raise_for_status()
                content, usage = self._parse_response(response)
                self.last_metrics = LLMCallMetrics(
                    model=self.model,
                    attempts=attempt,
                    latency_ms=round((time.monotonic() - started) * 1000),
                    input_chars=len(prompt) + (len(system) if system else 0),
                    usage=usage,
                    request_id=response.headers.get("x-request-id"),
                )
                log.info("llm call ok: model=%s attempts=%d latency_ms=%d",
                         self.model, attempt, self.last_metrics.latency_ms)
                return content, usage
            except httpx.HTTPStatusError as exc:
                # Preserve response/request for callers while keeping proxy body,
                # endpoint URL and request prompt out of logs and notifications.
                status = exc.response.status_code
                code = "unavailable"
                try:
                    data = exc.response.json()
                    if isinstance(data, dict):
                        code = _proxy_error_code(data) or code
                except ValueError:
                    pass
                last_exc = httpx.HTTPStatusError(
                    f"chat-completions HTTP {status} error_code={code}",
                    request=exc.request, response=exc.response,
                )
                if not _is_retryable_http_error(exc):
                    log.warning("llm call failed (attempt %d/%d): %s",
                                attempt, self.retry_max_attempts, last_exc)
                    raise last_exc from None
            except (httpx.TransportError, LLMResponseError) as exc:
                # Includes malformed 200 bodies from proxies and interrupted
                # transport streams. Both are transient in the deployed routes.
                last_exc = exc
            except ValueError as exc:
                # Defensive compatibility for custom httpx transports whose
                # ``json()`` implementation raises a plain ValueError.
                last_exc = exc

            log.warning("llm call failed (attempt %d/%d): %s",
                        attempt, self.retry_max_attempts, last_exc)
            if attempt < self.retry_max_attempts:
                self._sleep_before_retry(attempt=attempt, response=response)
        assert last_exc is not None
        if isinstance(last_exc, httpx.HTTPStatusError):
            raise last_exc from None
        raise last_exc
