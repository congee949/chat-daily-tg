"""Append-only shadow metrics and deterministic read-only canary routing.

This module deliberately has no dependency on Telegram, the delivery state
machine, or any fact source.  It only records derived observations emitted by
KnowledgeIndex operators and readers.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable


SCHEMA = "chatdaily-knowledge-shadow.v1"
GENERATION_CONTEXT_SCHEMA = "chatdaily-knowledge-generation-context.v1"
INCREMENTAL_REFRESH_PRODUCER = "incremental-refresh.v1"
ALLOWED_KINDS = {
    "health",
    "incremental",  # Legacy cursor/no-op evidence; readable but never gate-eligible.
    "incremental_refresh_receipt",
    "query",
    "source_freshness",
    "source_link",
}
_REQUEST_HASH = re.compile(r"[0-9a-f]{64}")
_GENERATION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_GENERATION_CONTEXT_KEYS = {
    "schema",
    "generation_id",
    "artifact_sha256",
    "manifest_hash",
    "catalog_hash",
    "vectors_hash",
    "model_id",
    "model_revision",
    "reranker_model_id",
    "reranker_revision",
    "dimension",
}
_INCREMENTAL_REFRESH_RECEIPT_REQUIRED_KEYS = {
    "schema",
    "kind",
    "timestamp",
    "generation_id",
    "generation_context",
    "producer",
    "success",
    "refresh_performed",
    "noop",
    "baseline_generation_id",
    "baseline_generation_context",
    "baseline_source_cursor_hash",
    "source_snapshot_hash",
    "output_generation_id",
    "output_source_cursor_hash",
}
_INCREMENTAL_REFRESH_RECEIPT_OPTIONAL_KEYS = {
    "output_generation_context",
    "error",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_time(value: Any) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("shadow event timestamp is required")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("shadow event timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def _safe_generation_id(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or (
        not value
        or value in {".", ".."}
        or Path(value).is_absolute()
        or "/" in value
        or "\\" in value
        or _GENERATION_ID.fullmatch(value) is None
    ):
        raise ValueError(f"shadow event requires a safe {field}")
    return value


def canary_selected(request_id: str, generation_id: str, *, percent: float = 10.0) -> bool:
    """Return a stable cohort decision without maintaining mutable state."""

    if not request_id.strip() or not generation_id.strip():
        raise ValueError("request_id and generation_id are required")
    if not math.isfinite(percent) or percent < 0.0 or percent > 100.0:
        raise ValueError("percent must be finite and between 0 and 100")
    digest = hashlib.sha256(f"{generation_id}\0{request_id}".encode("utf-8")).digest()
    bucket = int.from_bytes(digest[:8], "big") % 10_000
    return bucket < round(percent * 100)


def validate_generation_context(
    context: Any, *, generation_id: str
) -> dict[str, Any]:
    if not isinstance(context, dict):
        raise ValueError("shadow generation_context must be an object")
    value = dict(context)
    if set(value) != _GENERATION_CONTEXT_KEYS:
        raise ValueError("shadow generation_context fields are incomplete or unknown")
    if value.get("schema") != GENERATION_CONTEXT_SCHEMA:
        raise ValueError("shadow generation_context schema is invalid")
    if value.get("generation_id") != generation_id:
        raise ValueError("shadow generation_context generation_id mismatch")
    for field in (
        "artifact_sha256",
        "manifest_hash",
        "catalog_hash",
        "vectors_hash",
    ):
        if _REQUEST_HASH.fullmatch(str(value.get(field) or "")) is None:
            raise ValueError(f"shadow generation_context {field} must be SHA-256")
    for field in (
        "model_id",
        "model_revision",
        "reranker_model_id",
        "reranker_revision",
    ):
        if not isinstance(value.get(field), str) or not value[field].strip():
            raise ValueError(f"shadow generation_context {field} is required")
    dimension = value.get("dimension")
    if isinstance(dimension, bool) or not isinstance(dimension, int) or dimension < 1:
        raise ValueError("shadow generation_context dimension must be positive")
    return value


def _context_artifact_sha256(context: dict[str, Any]) -> str:
    """Recompute the immutable generation artifact binding used by the CLI."""

    immutable = {
        "generation_id": context["generation_id"],
        "manifest_hash": context["manifest_hash"],
        "catalog_hash": context["catalog_hash"],
        "vectors_hash": context["vectors_hash"],
    }
    return hashlib.sha256(
        json.dumps(immutable, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _validate_receipt_artifact_context(
    context: Any, *, generation_id: str, field: str
) -> dict[str, Any]:
    value = validate_generation_context(context, generation_id=generation_id)
    if value["artifact_sha256"] != _context_artifact_sha256(value):
        raise ValueError(f"incremental refresh {field} artifact identity mismatch")
    return value


def _validate_incremental_refresh_receipt(value: dict[str, Any]) -> None:
    keys = set(value)
    allowed = (
        _INCREMENTAL_REFRESH_RECEIPT_REQUIRED_KEYS
        | _INCREMENTAL_REFRESH_RECEIPT_OPTIONAL_KEYS
    )
    if not _INCREMENTAL_REFRESH_RECEIPT_REQUIRED_KEYS <= keys or not keys <= allowed:
        raise ValueError("incremental refresh receipt fields are incomplete or unknown")
    if value.get("producer") != INCREMENTAL_REFRESH_PRODUCER:
        raise ValueError(
            f"incremental refresh receipt producer must be {INCREMENTAL_REFRESH_PRODUCER}"
        )
    for field in ("success", "refresh_performed", "noop"):
        if not isinstance(value.get(field), bool):
            raise ValueError(f"incremental refresh receipt {field} must be boolean")
    for field in (
        "baseline_source_cursor_hash",
        "source_snapshot_hash",
        "output_source_cursor_hash",
    ):
        if not isinstance(value.get(field), str) or _REQUEST_HASH.fullmatch(value[field]) is None:
            raise ValueError(f"incremental refresh receipt {field} must be SHA-256")

    candidate_id = _safe_generation_id(
        value.get("generation_id"), field="generation_id"
    )
    candidate_context = _validate_receipt_artifact_context(
        value.get("generation_context"),
        generation_id=candidate_id,
        field="candidate",
    )
    baseline_id = _safe_generation_id(
        value.get("baseline_generation_id"), field="baseline_generation_id"
    )
    baseline_context = _validate_receipt_artifact_context(
        value.get("baseline_generation_context"),
        generation_id=baseline_id,
        field="baseline",
    )
    if baseline_id != candidate_id or baseline_context != candidate_context:
        raise ValueError(
            "incremental refresh baseline must match the shadow candidate artifact"
        )

    output_id = _safe_generation_id(
        value.get("output_generation_id"), field="output_generation_id"
    )
    if output_id == baseline_id:
        raise ValueError("incremental refresh output generation must differ from baseline")
    output_context: dict[str, Any] | None = None
    if "output_generation_context" in value:
        output_context = _validate_receipt_artifact_context(
            value["output_generation_context"],
            generation_id=output_id,
            field="output",
        )
        if output_context["artifact_sha256"] == baseline_context["artifact_sha256"]:
            raise ValueError("incremental refresh output artifact must differ from baseline")
        for field in (
            "model_id",
            "model_revision",
            "reranker_model_id",
            "reranker_revision",
            "dimension",
        ):
            if output_context[field] != baseline_context[field]:
                raise ValueError(
                    "incremental refresh output embedding context must match baseline"
                )

    if value["success"] is True:
        if value["refresh_performed"] is not True or value["noop"] is not False:
            raise ValueError(
                "successful incremental refresh receipt requires a real non-noop refresh"
            )
        if output_context is None:
            raise ValueError(
                "successful incremental refresh receipt requires output_generation_context"
            )
        if value["baseline_source_cursor_hash"] == value["source_snapshot_hash"]:
            raise ValueError(
                "successful incremental refresh receipt requires changed source cursors"
            )
        if value["output_source_cursor_hash"] != value["source_snapshot_hash"]:
            raise ValueError(
                "incremental refresh output cursor hash must match source snapshot"
            )
        if value.get("error") not in (None, ""):
            raise ValueError("successful incremental refresh receipt cannot contain an error")
    else:
        error = value.get("error")
        if not isinstance(error, str) or not error.strip():
            raise ValueError("failed incremental refresh receipt requires an error")


def validate_shadow_event(event: dict[str, Any]) -> dict[str, Any]:
    value = dict(event)
    if value.get("schema") not in (None, SCHEMA):
        raise ValueError(f"unknown shadow event schema: {value.get('schema')!r}")
    value["schema"] = SCHEMA
    kind = str(value.get("kind") or "")
    if kind not in ALLOWED_KINDS:
        raise ValueError(f"invalid shadow event kind: {kind!r}")
    generation_id = str(value.get("generation_id") or "")
    if not generation_id or "/" in generation_id or generation_id in {".", ".."}:
        raise ValueError("shadow event requires a safe generation_id")
    value["generation_id"] = generation_id
    if "generation_context" in value:
        value["generation_context"] = validate_generation_context(
            value["generation_context"], generation_id=generation_id
        )
    value["timestamp"] = value.get("timestamp") or utc_now()
    _parse_time(value["timestamp"])
    if kind == "health" and not isinstance(value.get("available"), bool):
        raise ValueError("health event requires boolean available")
    if kind == "incremental" and not isinstance(value.get("success"), bool):
        raise ValueError("incremental event requires boolean success")
    if kind == "source_freshness":
        if not isinstance(value.get("success"), bool):
            raise ValueError("source_freshness event requires boolean success")
        if not isinstance(value.get("noop"), bool):
            raise ValueError("source_freshness event requires boolean noop")
        producer = value.get("producer")
        if not isinstance(producer, str) or not producer.strip():
            raise ValueError("source_freshness event requires a producer")
    if kind == "incremental_refresh_receipt":
        _validate_incremental_refresh_receipt(value)
    if kind == "query":
        if value.get("route") not in {"candidate", "baseline"}:
            raise ValueError("query event route must be candidate or baseline")
        request_hash = str(value.get("request_hash") or "").casefold()
        if not _REQUEST_HASH.fullmatch(request_hash):
            raise ValueError("query event requires a SHA-256 request_hash")
        value["request_hash"] = request_hash
        latency = value.get("latency_ms")
        if (
            not isinstance(latency, (int, float))
            or not math.isfinite(float(latency))
            or latency < 0
        ):
            raise ValueError("query event requires finite non-negative latency_ms")
        if "reranker_error" in value and not isinstance(value["reranker_error"], bool):
            raise ValueError("query reranker_error must be boolean")
        if "reranker_attempted" in value and not isinstance(value["reranker_attempted"], bool):
            raise ValueError("query reranker_attempted must be boolean")
        if value.get("reranker_error") is True and value.get("reranker_attempted") is not True:
            raise ValueError("query reranker errors require a reranker attempt")
        canary_fields = {
            "selected_candidate",
            "served_route",
            "candidate_latency_ms",
            "total_latency_ms",
        }
        present = canary_fields.intersection(value)
        if present and present != canary_fields:
            raise ValueError("query canary telemetry fields must be complete")
        if present:
            if not isinstance(value["selected_candidate"], bool):
                raise ValueError("query selected_candidate must be boolean")
            if value["served_route"] not in {"candidate", "baseline"}:
                raise ValueError("query served_route must be candidate or baseline")
            if value["route"] != value["served_route"]:
                raise ValueError("query route must match served_route")
            for field in ("candidate_latency_ms", "total_latency_ms"):
                latency_value = value[field]
                if (
                    isinstance(latency_value, bool)
                    or not isinstance(latency_value, (int, float))
                    or not math.isfinite(float(latency_value))
                    or latency_value < 0
                ):
                    raise ValueError(f"query {field} must be finite and non-negative")
            if float(value["candidate_latency_ms"]) > float(value["total_latency_ms"]):
                raise ValueError("query candidate latency cannot exceed total latency")
            if float(value["total_latency_ms"]) != float(value["latency_ms"]):
                raise ValueError("query latency_ms must equal total_latency_ms")
            if value["selected_candidate"] is False:
                if value["served_route"] != "baseline":
                    raise ValueError("unselected canary query must serve baseline")
                if float(value["candidate_latency_ms"]) != 0.0:
                    raise ValueError("unselected canary query cannot have candidate latency")
                if value.get("fallback_reason"):
                    raise ValueError("unselected canary query cannot have a fallback")
            elif value["served_route"] == "baseline" and not value.get("fallback_reason"):
                raise ValueError("candidate fallback requires fallback_reason")
            elif value["served_route"] == "candidate" and value.get("fallback_reason"):
                raise ValueError("candidate-served query cannot have fallback_reason")
    if kind == "source_link" and not isinstance(value.get("accurate"), bool):
        raise ValueError("source_link event requires boolean accurate")
    return value


def append_shadow_event(path: Path, event: dict[str, Any]) -> dict[str, Any]:
    """Append and fsync one validated event under an advisory file lock."""

    value = validate_shadow_event(event)
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            handle.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except Exception:
        # fdopen owns the descriptor after successful construction.
        raise
    return value


def load_shadow_events(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid shadow JSON at {path}:{line_number}") from exc
            if not isinstance(raw, dict):
                raise ValueError(f"shadow event is not an object at {path}:{line_number}")
            rows.append(validate_shadow_event(raw))
    return rows


def _daily_boolean_observations(
    events: Iterable[dict[str, Any]], *, kind: str, field: str
) -> list[dict[str, Any]]:
    """Collapse retries into one pessimistic observation per UTC date."""

    buckets: dict[str, bool] = {}
    for event in events:
        if event["kind"] != kind:
            continue
        date = _parse_time(event["timestamp"]).date().isoformat()
        outcome = bool(event[field])
        buckets[date] = bool(buckets.get(date, True) and outcome)
    return [{"date": date, field: buckets[date]} for date in sorted(buckets)]


def _daily_incremental_refreshes(
    events: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return one pessimistic authoritative refresh receipt per UTC date.

    Cursor-consistent ``incremental`` and ``source_freshness`` observations do
    not prove that chunks/vectors were rebuilt.  Likewise, a no-op receipt is
    retained for diagnosis but is not gate evidence.  For real receipts, any
    failed observation dominates all successful retries on the same UTC date.
    """

    buckets: dict[str, bool] = {}
    for event in events:
        if event["kind"] != "incremental_refresh_receipt":
            continue
        if event.get("producer") != INCREMENTAL_REFRESH_PRODUCER:
            # validate_shadow_event rejects this on load; retain an explicit
            # defense here so callers of this helper cannot widen authority.
            continue
        if event["noop"] is True:
            continue
        date = _parse_time(event["timestamp"]).date().isoformat()
        outcome = bool(
            event["success"] is True
            and event["refresh_performed"] is True
            and event["noop"] is False
        )
        buckets[date] = bool(buckets.get(date, True) and outcome)
    return [{"date": date, "success": buckets[date]} for date in sorted(buckets)]


def _consecutive_incremental_successes(
    daily_refreshes: Iterable[dict[str, Any]],
) -> int:
    ordered = list(daily_refreshes)
    count = 0
    previous_date: datetime | None = None
    for event in reversed(ordered):
        if not event["success"]:
            break
        date = datetime.fromisoformat(event["date"])
        if previous_date is not None and (previous_date - date).days != 1:
            break
        count += 1
        previous_date = date
    return count


def _percentile(values: list[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(quantile * len(ordered)) - 1))
    return float(ordered[index])


def _candidate_attempted(event: dict[str, Any]) -> bool:
    if "selected_candidate" in event:
        return event["selected_candidate"] is True
    # Legacy v1 events encoded an attempted candidate either as a candidate
    # route or as a baseline route carrying a candidate fallback reason.
    return event["route"] == "candidate" or bool(event.get("fallback_reason"))


def _served_route(event: dict[str, Any]) -> str:
    return str(event.get("served_route") or event["route"])


def _total_latency_ms(event: dict[str, Any]) -> float:
    return float(event.get("total_latency_ms", event["latency_ms"]))


def _candidate_latency_ms(event: dict[str, Any]) -> float:
    if not _candidate_attempted(event):
        return 0.0
    return float(event.get("candidate_latency_ms", event["latency_ms"]))


def guard_shadow_metrics(
    path: Path,
    generation_id: str,
    *,
    now: datetime | None = None,
    window_minutes: int = 10,
    p95_window_count: int = 3,
    source_link_window_days: int = 7,
    generation_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Derive time-bounded rollback observations from the append-only journal.

    Query retries are deduplicated before windowing and retain their worst
    latency/error state.  Every p95 window must contain a real candidate
    attempt; missing telemetry is an invalid snapshot, never a healthy zero.
    """

    if window_minutes < 1 or p95_window_count != 3 or source_link_window_days < 1:
        raise ValueError("guard shadow window configuration is invalid")
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    expected_context = (
        validate_generation_context(generation_context, generation_id=generation_id)
        if generation_context is not None
        else None
    )
    query_start = current - timedelta(minutes=window_minutes * p95_window_count)
    link_start = current - timedelta(days=source_link_window_days)
    rows = [
        row
        for row in load_shadow_events(path)
        if row["generation_id"] == generation_id
        and (
            expected_context is None
            or row.get("generation_context") == expected_context
        )
        and link_start <= _parse_time(row["timestamp"]) <= current
    ]
    raw_queries = [
        row
        for row in rows
        if row["kind"] == "query" and _parse_time(row["timestamp"]) >= query_start
    ]
    queries, duplicates, conflicts = _deduplicate_queries(raw_queries)
    if conflicts:
        raise ValueError("guard query telemetry contains request route conflicts")
    candidate_attempts = [row for row in queries if _candidate_attempted(row)]
    for row in candidate_attempts:
        if not isinstance(row.get("reranker_attempted"), bool):
            raise ValueError(
                "guard query telemetry requires reranker_attempted on every candidate attempt"
            )
        if not isinstance(row.get("reranker_error"), bool):
            raise ValueError(
                "guard query telemetry requires reranker_error on every candidate attempt"
            )

    p95_windows: list[dict[str, Any]] = []
    for index in range(p95_window_count):
        start = query_start + timedelta(minutes=window_minutes * index)
        end = start + timedelta(minutes=window_minutes)
        values = [
            _total_latency_ms(row)
            for row in candidate_attempts
            if start <= _parse_time(row["timestamp"])
            and (
                _parse_time(row["timestamp"]) < end
                or (index == p95_window_count - 1 and _parse_time(row["timestamp"]) <= end)
            )
        ]
        if not values:
            raise ValueError(f"guard p95 window {index + 1} has no candidate query samples")
        p95_windows.append(
            {
                "start": start.isoformat(),
                "end": end.isoformat(),
                "sample_count": len(values),
                "p95_ms": _percentile(values, 0.95),
            }
        )

    reranker_start = current - timedelta(minutes=window_minutes)
    recent_attempts = [
        row
        for row in candidate_attempts
        if row["reranker_attempted"] is True
        and reranker_start <= _parse_time(row["timestamp"]) <= current
    ]
    if not recent_attempts:
        raise ValueError("guard reranker window has no reranker attempt samples")
    reranker_errors = sum(1 for row in recent_attempts if row["reranker_error"] is True)

    link_days = _daily_boolean_observations(
        rows, kind="source_link", field="accurate"
    )
    if not link_days:
        raise ValueError("guard source-link window has no audits")
    bad_links = sum(1 for row in link_days if row["accurate"] is not True)
    return {
        "query_duplicates": duplicates,
        "reranker": {
            "start": reranker_start.isoformat(),
            "end": current.isoformat(),
            "request_count": len(recent_attempts),
            "error_count": reranker_errors,
            "error_rate": reranker_errors / len(recent_attempts),
        },
        "p95_windows": p95_windows,
        "source_links": {
            "start": link_start.isoformat(),
            "end": current.isoformat(),
            "audit_count": len(link_days),
            "bad_count": bad_links,
        },
    }


def _hourly_health(events: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse health probes into real UTC-hour observations.

    A release needs observations distributed across the seven-day window, not
    168 calls made in one burst.  Multiple probes in one hour therefore count
    once, and any failure makes that hour unavailable so duplicate healthy
    samples cannot dilute an observed outage.
    """

    buckets: dict[datetime, dict[str, Any]] = {}
    for event in events:
        timestamp = _parse_time(event["timestamp"])
        hour = timestamp.replace(minute=0, second=0, microsecond=0)
        previous = buckets.get(hour)
        if previous is None:
            buckets[hour] = {
                "timestamp": hour.isoformat(),
                "available": bool(event["available"]),
            }
        else:
            previous["available"] = bool(previous["available"] and event["available"])
    return [buckets[key] for key in sorted(buckets)]


def _deduplicate_queries(
    events: Iterable[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int, int]:
    """Count each stable request once while retaining worst observed behavior."""

    unique: dict[str, dict[str, Any]] = {}
    duplicates = 0
    conflicts = 0
    for event in events:
        request_hash = str(event["request_hash"])
        previous = unique.get(request_hash)
        if previous is None:
            unique[request_hash] = dict(event)
            continue
        duplicates += 1
        previous_selected = _candidate_attempted(previous)
        event_selected = _candidate_attempted(event)
        if previous_selected != event_selected:
            conflicts += 1
        elif (
            "selected_candidate" not in previous
            and "selected_candidate" not in event
            and previous["route"] != event["route"]
        ):
            # Old records cannot distinguish stable cohort selection from the
            # ultimately served route, so a route change remains ambiguous.
            conflicts += 1
        # A retry of one request must never improve the release evidence.
        worst_total = max(_total_latency_ms(previous), _total_latency_ms(event))
        previous["latency_ms"] = worst_total
        if "total_latency_ms" in previous or "total_latency_ms" in event:
            previous["total_latency_ms"] = worst_total
            previous["candidate_latency_ms"] = max(
                _candidate_latency_ms(previous), _candidate_latency_ms(event)
            )
            previous["selected_candidate"] = previous_selected or event_selected
            served = (
                "baseline"
                if (
                    previous.get("fallback_reason")
                    or event.get("fallback_reason")
                    or _served_route(previous) == "baseline"
                    or _served_route(event) == "baseline"
                )
                else "candidate"
            )
            previous["served_route"] = served
            previous["route"] = served
        previous["reranker_error"] = bool(
            previous.get("reranker_error") is True or event.get("reranker_error") is True
        )
        previous["reranker_attempted"] = bool(
            previous.get("reranker_attempted") is True or event.get("reranker_attempted") is True
        )
        if event.get("fallback_reason") and not previous.get("fallback_reason"):
            previous["fallback_reason"] = event["fallback_reason"]
    return list(unique.values()), duplicates, conflicts


def summarize_shadow(
    path: Path,
    generation_id: str,
    *,
    now: datetime | None = None,
    window_days: int = 7,
    generation_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if window_days < 1:
        raise ValueError("window_days must be positive")
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    expected_context = (
        validate_generation_context(generation_context, generation_id=generation_id)
        if generation_context is not None
        else None
    )
    cutoff = current - timedelta(days=window_days)
    generation_rows = [
        row
        for row in load_shadow_events(path)
        if row["generation_id"] == generation_id
        and cutoff <= _parse_time(row["timestamp"]) <= current
    ]
    rows = [
        row
        for row in generation_rows
        if expected_context is None or row.get("generation_context") == expected_context
    ]
    rows.sort(key=lambda row: _parse_time(row["timestamp"]))
    raw_health = [row for row in rows if row["kind"] == "health"]
    health = _hourly_health(raw_health)
    raw_query = [row for row in rows if row["kind"] == "query"]
    query, duplicate_queries, query_hash_conflicts = _deduplicate_queries(raw_query)
    link_days = _daily_boolean_observations(
        rows, kind="source_link", field="accurate"
    )
    raw_incremental_refreshes = [
        row for row in rows if row["kind"] == "incremental_refresh_receipt"
    ]
    incremental_refresh_days = _daily_incremental_refreshes(
        raw_incremental_refreshes
    )
    legacy_incremental_events = sum(
        1 for row in rows if row["kind"] == "incremental"
    )
    source_freshness_events = sum(
        1 for row in rows if row["kind"] == "source_freshness"
    )
    noop_refresh_receipts = sum(
        1 for row in raw_incremental_refreshes if row["noop"] is True
    )
    failed_refresh_days = sum(
        1 for row in incremental_refresh_days if row["success"] is not True
    )
    available = sum(1 for row in health if row["available"])
    availability = available / len(health) if health else 0.0
    candidate_rows = [row for row in query if _candidate_attempted(row)]
    candidate = len(candidate_rows)
    reranker_rows = [row for row in candidate_rows if row.get("reranker_attempted") is True]
    reranker_errors = sum(1 for row in reranker_rows if row.get("reranker_error") is True)
    candidate_fallbacks = sum(
        1
        for row in candidate_rows
        if _served_route(row) == "baseline" or row.get("fallback_reason")
    )
    observed_dates = sorted({_parse_time(row["timestamp"]).date().isoformat() for row in health})
    span_seconds = 0.0
    if health:
        span_seconds = (
            _parse_time(health[-1]["timestamp"]) - _parse_time(health[0]["timestamp"])
        ).total_seconds()
    consecutive = _consecutive_incremental_successes(incremental_refresh_days)
    bad_links = sum(1 for row in link_days if not row["accurate"])
    gates: list[str] = []
    # Seven distinct UTC dates plus a true six-day span prevents a burst of
    # backdated same-day samples from being called a seven-day observation.
    # Requiring 168 distinct UTC-hour buckets additionally prevents concentrated
    # probes from manufacturing the hourly availability evidence.
    if len(observed_dates) < 7 or span_seconds < 6 * 24 * 60 * 60:
        gates.append("shadow_below_7_days")
    minimum_health_samples = window_days * 24
    if len(health) < minimum_health_samples:
        gates.append(f"health_samples_below_{minimum_health_samples}")
    if availability < 0.995:
        gates.append("availability_below_0.995")
    if consecutive < 7:
        gates.append("authoritative_incremental_refreshes_below_7")
        if legacy_incremental_events:
            gates.append("legacy_incremental_events_are_not_refresh_receipts")
        if source_freshness_events:
            gates.append("source_freshness_events_are_not_refresh_receipts")
        if noop_refresh_receipts:
            gates.append("noop_refresh_receipts_are_not_refresh_evidence")
        if failed_refresh_days:
            gates.append("failed_incremental_refresh_receipts_observed")
    if len(link_days) < 7:
        gates.append("source_link_audits_below_7")
    if bad_links:
        gates.append("bad_source_links")

    canary_gates: list[str] = []
    candidate_ratio = candidate / len(query) if query else 0.0
    if len(query) < 100 or len(candidate_rows) < 5:
        canary_gates.append("canary_samples_insufficient")
    if query and not 0.05 <= candidate_ratio <= 0.15:
        canary_gates.append("canary_ratio_outside_5_to_15_percent")
    if candidate_rows and not reranker_rows:
        canary_gates.append("reranker_samples_missing")
    reranker_error_rate = reranker_errors / len(reranker_rows) if reranker_rows else 0.0
    if reranker_error_rate > 0.02:
        canary_gates.append("reranker_error_rate_over_0.02")
    candidate_p95_ms = _percentile(
        [_total_latency_ms(row) for row in candidate_rows], 0.95
    )
    if candidate_p95_ms > 8000.0:
        canary_gates.append("candidate_p95_over_8s")
    if candidate_fallbacks:
        canary_gates.append("candidate_fallbacks_observed")
    if query_hash_conflicts:
        canary_gates.append("query_request_hash_conflicts")
    if bad_links:
        canary_gates.append("bad_source_links")
    return {
        "schema": "chatdaily-knowledge-shadow-summary.v1",
        "generation_id": generation_id,
        "window_days": window_days,
        "window_start": cutoff.isoformat(),
        "window_end": current.isoformat(),
        "event_count": len(rows),
        "context_mismatch_events": len(generation_rows) - len(rows),
        "generation_context": expected_context,
        "health_events": len(raw_health),
        "health_samples": len(health),
        "observed_dates": observed_dates,
        "observed_span_seconds": span_seconds,
        "availability": availability,
        "incremental_consecutive_successes": consecutive,
        "incremental_refresh_receipts": len(raw_incremental_refreshes),
        "incremental_refresh_days": len(incremental_refresh_days),
        "incremental_refresh_failed_days": failed_refresh_days,
        "legacy_incremental_events_ignored": legacy_incremental_events,
        "source_freshness_events_ignored": source_freshness_events,
        "noop_incremental_refresh_receipts_ignored": noop_refresh_receipts,
        "query_events": len(raw_query),
        "query_samples": len(query),
        "duplicate_query_samples": duplicate_queries,
        "query_request_hash_conflicts": query_hash_conflicts,
        "candidate_queries": candidate,
        "candidate_ratio": candidate_ratio,
        "candidate_p95_ms": candidate_p95_ms,
        "candidate_fallbacks": candidate_fallbacks,
        "reranker_error_rate": reranker_error_rate,
        "reranker_requests": len(reranker_rows),
        "source_link_audits": len(link_days),
        "bad_source_links": bad_links,
        "ready": not gates,
        "failure_reasons": gates,
        "canary_ready": not canary_gates,
        "canary_failure_reasons": canary_gates,
    }
