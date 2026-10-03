from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest

from chat_daily_tg.knowledge_shadow import (
    GENERATION_CONTEXT_SCHEMA,
    INCREMENTAL_REFRESH_PRODUCER,
    append_shadow_event,
    canary_selected,
    guard_shadow_metrics,
    load_shadow_events,
    summarize_shadow,
)


def _generation_context(generation_id: str, artifact: str) -> dict[str, object]:
    manifest_hash = hashlib.sha256(f"{artifact}:manifest".encode()).hexdigest()
    catalog_hash = hashlib.sha256(f"{artifact}:catalog".encode()).hexdigest()
    vectors_hash = hashlib.sha256(f"{artifact}:vectors".encode()).hexdigest()
    immutable = {
        "generation_id": generation_id,
        "manifest_hash": manifest_hash,
        "catalog_hash": catalog_hash,
        "vectors_hash": vectors_hash,
    }
    artifact_sha256 = hashlib.sha256(
        json.dumps(immutable, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {
        "schema": GENERATION_CONTEXT_SCHEMA,
        "generation_id": generation_id,
        "artifact_sha256": artifact_sha256,
        "manifest_hash": manifest_hash,
        "catalog_hash": catalog_hash,
        "vectors_hash": vectors_hash,
        "model_id": "embed-test",
        "model_revision": "embed-revision",
        "reranker_model_id": "rerank-test",
        "reranker_revision": "rerank-revision",
        "dimension": 4096,
    }


def _refresh_receipt(
    candidate_context: dict[str, object],
    output_generation_id: str,
    artifact: str,
    *,
    timestamp: datetime,
    success: bool = True,
    refresh_performed: bool | None = None,
    noop: bool = False,
    include_output_context: bool = True,
) -> dict[str, object]:
    source_snapshot_hash = hashlib.sha256(
        f"snapshot:{artifact}".encode()
    ).hexdigest()
    receipt: dict[str, object] = {
        "kind": "incremental_refresh_receipt",
        "producer": INCREMENTAL_REFRESH_PRODUCER,
        "generation_id": candidate_context["generation_id"],
        "generation_context": candidate_context,
        "timestamp": timestamp.isoformat(),
        "success": success,
        "refresh_performed": success if refresh_performed is None else refresh_performed,
        "noop": noop,
        "baseline_generation_id": candidate_context["generation_id"],
        "baseline_generation_context": candidate_context,
        "baseline_source_cursor_hash": hashlib.sha256(
            f"baseline:{artifact}".encode()
        ).hexdigest(),
        "source_snapshot_hash": source_snapshot_hash,
        "output_generation_id": output_generation_id,
        "output_source_cursor_hash": source_snapshot_hash,
    }
    if include_output_context:
        receipt["output_generation_context"] = _generation_context(
            output_generation_id, artifact
        )
    if not success:
        receipt["error"] = "refresh_failed"
    return receipt


def _request_hash(index: int) -> str:
    return hashlib.sha256(f"request-{index}".encode()).hexdigest()


def test_canary_is_stable_and_close_to_ten_percent() -> None:
    first = [canary_selected(str(index), "gen-a") for index in range(10_000)]
    second = [canary_selected(str(index), "gen-a") for index in range(10_000)]

    assert first == second
    assert 900 <= sum(first) <= 1100
    assert not canary_selected("request", "gen-a", percent=0)
    assert canary_selected("request", "gen-a", percent=100)


@pytest.mark.parametrize("percent", [-0.1, 100.1, float("nan")])
def test_canary_rejects_invalid_percent(percent: float) -> None:
    with pytest.raises(ValueError):
        canary_selected("request", "gen", percent=percent)


def test_shadow_journal_requires_real_seven_day_observation(tmp_path) -> None:
    journal = tmp_path / "shadow.jsonl"
    now = datetime(2026, 8, 25, 12, tzinfo=timezone.utc)
    candidate_context = _generation_context("gen-a", "candidate")
    for hours_ago in range(168):
        append_shadow_event(
            journal,
            {
                "kind": "health",
                "generation_id": "gen-a",
                "timestamp": (now - timedelta(hours=hours_ago)).isoformat(),
                "available": True,
            },
        )
    for days_ago in range(7):
        timestamp = now - timedelta(days=days_ago)
        append_shadow_event(
            journal,
            _refresh_receipt(
                candidate_context,
                f"gen-output-{days_ago}",
                f"output-{days_ago}",
                timestamp=timestamp,
            ),
        )
        append_shadow_event(
            journal,
            {
                "kind": "source_link",
                "generation_id": "gen-a",
                "timestamp": timestamp.isoformat(),
                "accurate": True,
            },
        )

    summary = summarize_shadow(journal, "gen-a", now=now)

    assert summary["ready"] is True
    assert summary["availability"] == 1.0
    assert summary["incremental_consecutive_successes"] == 7
    assert summary["incremental_refresh_days"] == 7
    assert len(summary["observed_dates"]) >= 7


def test_shadow_summary_rejects_old_evidence_after_same_id_artifact_rebuild(
    tmp_path: Path,
) -> None:
    journal = tmp_path / "shadow.jsonl"
    now = datetime(2026, 8, 25, 12, tzinfo=timezone.utc)
    old_context = _generation_context("gen-a", "a")
    rebuilt_context = _generation_context("gen-a", "e")
    for context in (old_context, rebuilt_context):
        append_shadow_event(
            journal,
            {
                "kind": "health",
                "generation_id": "gen-a",
                "timestamp": now.isoformat(),
                "available": True,
                "generation_context": context,
            },
        )

    summary = summarize_shadow(
        journal,
        "gen-a",
        now=now,
        generation_context=rebuilt_context,
    )

    assert summary["event_count"] == 1
    assert summary["context_mismatch_events"] == 1
    assert summary["generation_context"] == rebuilt_context


def test_duplicate_authoritative_refresh_receipts_do_not_inflate_utc_days(
    tmp_path,
) -> None:
    journal = tmp_path / "shadow.jsonl"
    now = datetime(2026, 8, 25, 12, tzinfo=timezone.utc)
    candidate_context = _generation_context("gen-a", "candidate")
    for days_ago in range(7):
        receipt = _refresh_receipt(
            candidate_context,
            f"gen-output-{days_ago}",
            f"output-{days_ago}",
            timestamp=now - timedelta(days=days_ago),
        )
        append_shadow_event(journal, receipt)
        append_shadow_event(journal, receipt)

    summary = summarize_shadow(journal, "gen-a", now=now)

    assert summary["incremental_refresh_receipts"] == 14
    assert summary["incremental_refresh_days"] == 7
    assert summary["incremental_consecutive_successes"] == 7


def test_failed_refresh_receipt_may_omit_output_context_but_requires_error(
    tmp_path,
) -> None:
    journal = tmp_path / "shadow.jsonl"
    now = datetime(2026, 8, 25, 12, tzinfo=timezone.utc)
    candidate_context = _generation_context("gen-a", "candidate")
    failed = _refresh_receipt(
        candidate_context,
        "gen-output-failed",
        "failed-output",
        timestamp=now,
        success=False,
        include_output_context=False,
    )

    assert append_shadow_event(journal, failed)["success"] is False
    without_error = dict(failed)
    without_error.pop("error")
    with pytest.raises(ValueError, match="requires an error"):
        append_shadow_event(journal, without_error)


@pytest.mark.parametrize(
    ("mutate", "error"),
    [
        (
            lambda row: row.update(producer="manual-refresh.v1"),
            "producer must be incremental-refresh.v1",
        ),
        (
            lambda row: row["generation_context"].update(
                artifact_sha256="f" * 64
            ),
            "candidate artifact identity mismatch",
        ),
        (
            lambda row: row.update(
                baseline_generation_context=_generation_context(
                    "gen-a", "different-baseline"
                )
            ),
            "baseline must match the shadow candidate artifact",
        ),
        (
            lambda row: row["output_generation_context"].update(
                generation_id="other-output"
            ),
            "generation_id mismatch",
        ),
        (
            lambda row: row["output_generation_context"].update(
                artifact_sha256="e" * 64
            ),
            "output artifact identity mismatch",
        ),
        (
            lambda row: row.update(
                baseline_source_cursor_hash=row["source_snapshot_hash"]
            ),
            "requires changed source cursors",
        ),
        (
            lambda row: row.update(output_source_cursor_hash="9" * 64),
            "output cursor hash must match source snapshot",
        ),
    ],
)
def test_incremental_refresh_receipt_rejects_artifact_or_context_tamper(
    tmp_path, mutate, error: str
) -> None:
    candidate_context = _generation_context("gen-a", "candidate")
    receipt = _refresh_receipt(
        candidate_context,
        "gen-output",
        "output",
        timestamp=datetime(2026, 8, 25, 12, tzinfo=timezone.utc),
    )
    tampered = copy.deepcopy(receipt)
    mutate(tampered)

    with pytest.raises(ValueError, match=error):
        append_shadow_event(tmp_path / "shadow.jsonl", tampered)


def test_shadow_journal_fails_closed_on_bad_link_and_availability(tmp_path) -> None:
    journal = tmp_path / "shadow.jsonl"
    now = datetime(2026, 8, 25, 12, tzinfo=timezone.utc)
    for hours_ago in range(168):
        timestamp = now - timedelta(hours=hours_ago)
        append_shadow_event(
            journal,
            {
                "kind": "health",
                "generation_id": "gen-a",
                "timestamp": timestamp.isoformat(),
                "available": hours_ago != 3,
            },
        )
    for days_ago in range(7):
        timestamp = now - timedelta(days=days_ago)
        append_shadow_event(
            journal,
            {
                "kind": "incremental",
                "generation_id": "gen-a",
                "timestamp": timestamp.isoformat(),
                "success": True,
            },
        )
    append_shadow_event(
        journal,
        {
            "kind": "source_link",
            "generation_id": "gen-a",
            "timestamp": now.isoformat(),
            "accurate": False,
        },
    )

    summary = summarize_shadow(journal, "gen-a", now=now)

    assert summary["ready"] is False
    assert "availability_below_0.995" in summary["failure_reasons"]
    assert "bad_source_links" in summary["failure_reasons"]


def test_shadow_journal_is_strict_jsonl(tmp_path) -> None:
    journal = tmp_path / "shadow.jsonl"
    journal.write_text('{"kind":', encoding="utf-8")

    with pytest.raises(ValueError, match="invalid shadow JSON"):
        load_shadow_events(journal)


def test_canary_readiness_requires_real_sample_volume_and_latency(tmp_path) -> None:
    journal = tmp_path / "shadow.jsonl"
    now = datetime(2026, 8, 25, 12, tzinfo=timezone.utc)
    for index in range(100):
        append_shadow_event(
            journal,
            {
                "kind": "query",
                "generation_id": "gen-a",
                "timestamp": (now - timedelta(minutes=index)).isoformat(),
                "route": "candidate" if index < 10 else "baseline",
                "latency_ms": 200.0,
                "reranker_attempted": True,
                "reranker_error": False,
                "request_hash": _request_hash(index),
            },
        )

    summary = summarize_shadow(journal, "gen-a", now=now)

    assert summary["canary_ready"] is True
    assert summary["candidate_ratio"] == 0.1
    assert summary["candidate_p95_ms"] == 200.0


def test_canary_duplicate_request_hashes_count_once_and_keep_worst_result(tmp_path) -> None:
    journal = tmp_path / "shadow.jsonl"
    now = datetime(2026, 8, 25, 12, tzinfo=timezone.utc)
    for index in range(100):
        append_shadow_event(
            journal,
            {
                "kind": "query",
                "generation_id": "gen-a",
                "timestamp": (now - timedelta(minutes=index)).isoformat(),
                "route": "candidate" if index < 10 else "baseline",
                "latency_ms": 200.0,
                "reranker_attempted": True,
                "reranker_error": False,
                "request_hash": _request_hash(index),
            },
        )
    append_shadow_event(
        journal,
        {
            "kind": "query",
            "generation_id": "gen-a",
            "timestamp": now.isoformat(),
            "route": "candidate",
            "latency_ms": 9000.0,
            "reranker_attempted": True,
            "reranker_error": True,
            "request_hash": _request_hash(0),
        },
    )

    summary = summarize_shadow(journal, "gen-a", now=now)

    assert summary["query_events"] == 101
    assert summary["query_samples"] == 100
    assert summary["duplicate_query_samples"] == 1
    assert summary["candidate_p95_ms"] == 9000.0
    assert summary["reranker_error_rate"] == 0.1
    assert summary["canary_ready"] is False
    assert "candidate_p95_over_8s" in summary["canary_failure_reasons"]
    assert "reranker_error_rate_over_0.02" in summary["canary_failure_reasons"]


def test_canary_rejects_missing_or_malformed_request_hash(tmp_path) -> None:
    journal = tmp_path / "shadow.jsonl"
    base = {
        "kind": "query",
        "generation_id": "gen-a",
        "route": "baseline",
        "latency_ms": 100.0,
    }

    with pytest.raises(ValueError, match="SHA-256 request_hash"):
        append_shadow_event(journal, base)
    with pytest.raises(ValueError, match="SHA-256 request_hash"):
        append_shadow_event(journal, {**base, "request_hash": "same-request"})


def test_canary_telemetry_accepts_complete_attempt_and_fallback_rows(tmp_path) -> None:
    journal = tmp_path / "shadow.jsonl"
    candidate = {
        "kind": "query",
        "generation_id": "gen-a",
        "route": "candidate",
        "request_hash": _request_hash(1),
        "latency_ms": 240.0,
        "selected_candidate": True,
        "served_route": "candidate",
        "candidate_latency_ms": 220.0,
        "total_latency_ms": 240.0,
    }
    fallback = {
        **candidate,
        "route": "baseline",
        "request_hash": _request_hash(2),
        "latency_ms": 410.0,
        "served_route": "baseline",
        "candidate_latency_ms": 300.0,
        "total_latency_ms": 410.0,
        "fallback_reason": "TimeoutError: candidate deadline",
    }

    assert append_shadow_event(journal, candidate)["selected_candidate"] is True
    assert append_shadow_event(journal, fallback)["served_route"] == "baseline"
    assert len(load_shadow_events(journal)) == 2


@pytest.mark.parametrize(
    ("mutate", "error"),
    [
        (lambda row: row.pop("total_latency_ms"), "fields must be complete"),
        (
            lambda row: row.update(selected_candidate=False, served_route="candidate"),
            "unselected canary query",
        ),
        (
            lambda row: row.update(candidate_latency_ms=201.0),
            "cannot exceed total latency",
        ),
        (
            lambda row: row.update(total_latency_ms=199.0),
            "must equal total_latency_ms",
        ),
        (
            lambda row: row.update(route="baseline", served_route="baseline"),
            "requires fallback_reason",
        ),
        (
            lambda row: row.update(fallback_reason="unexpected"),
            "cannot have fallback_reason",
        ),
    ],
)
def test_canary_telemetry_rejects_partial_or_inconsistent_rows(
    tmp_path, mutate, error: str
) -> None:
    row = {
        "kind": "query",
        "generation_id": "gen-a",
        "route": "candidate",
        "request_hash": _request_hash(1),
        "latency_ms": 200.0,
        "selected_candidate": True,
        "served_route": "candidate",
        "candidate_latency_ms": 180.0,
        "total_latency_ms": 200.0,
    }
    mutate(row)

    with pytest.raises(ValueError, match=error):
        append_shadow_event(tmp_path / "shadow.jsonl", row)


def test_selected_fallback_counts_as_candidate_but_unselected_baseline_does_not(
    tmp_path,
) -> None:
    journal = tmp_path / "shadow.jsonl"
    now = datetime(2026, 8, 25, 12, tzinfo=timezone.utc)
    append_shadow_event(
        journal,
        {
            "kind": "query",
            "generation_id": "gen-a",
            "timestamp": now.isoformat(),
            "route": "baseline",
            "request_hash": _request_hash(1),
            "latency_ms": 500.0,
            "selected_candidate": True,
            "served_route": "baseline",
            "candidate_latency_ms": 300.0,
            "total_latency_ms": 500.0,
            "fallback_reason": "RuntimeError: candidate unavailable",
        },
    )
    append_shadow_event(
        journal,
        {
            "kind": "query",
            "generation_id": "gen-a",
            "timestamp": now.isoformat(),
            "route": "baseline",
            "request_hash": _request_hash(2),
            "latency_ms": 100.0,
            "selected_candidate": False,
            "served_route": "baseline",
            "candidate_latency_ms": 0.0,
            "total_latency_ms": 100.0,
        },
    )

    summary = summarize_shadow(journal, "gen-a", now=now)

    assert summary["query_samples"] == 2
    assert summary["candidate_queries"] == 1
    assert summary["candidate_ratio"] == 0.5
    assert summary["candidate_fallbacks"] == 1
    assert summary["candidate_p95_ms"] == 500.0


def test_legacy_baseline_fallback_still_counts_as_candidate_attempt(tmp_path) -> None:
    journal = tmp_path / "shadow.jsonl"
    now = datetime(2026, 8, 25, 12, tzinfo=timezone.utc)
    append_shadow_event(
        journal,
        {
            "kind": "query",
            "generation_id": "gen-a",
            "timestamp": now.isoformat(),
            "route": "baseline",
            "request_hash": _request_hash(1),
            "latency_ms": 350.0,
            "fallback_reason": "ValueError: invalid candidate",
        },
    )

    summary = summarize_shadow(journal, "gen-a", now=now)

    assert summary["candidate_queries"] == 1
    assert summary["candidate_fallbacks"] == 1
    assert summary["candidate_p95_ms"] == 350.0


def test_duplicate_canary_retry_keeps_worst_total_latency_and_fallback(tmp_path) -> None:
    journal = tmp_path / "shadow.jsonl"
    now = datetime(2026, 8, 25, 12, tzinfo=timezone.utc)
    common = {
        "kind": "query",
        "generation_id": "gen-a",
        "timestamp": now.isoformat(),
        "request_hash": _request_hash(1),
        "selected_candidate": True,
        "reranker_attempted": True,
        "reranker_error": False,
    }
    append_shadow_event(
        journal,
        {
            **common,
            "route": "candidate",
            "latency_ms": 100.0,
            "served_route": "candidate",
            "candidate_latency_ms": 80.0,
            "total_latency_ms": 100.0,
        },
    )
    append_shadow_event(
        journal,
        {
            **common,
            "route": "baseline",
            "latency_ms": 400.0,
            "served_route": "baseline",
            "candidate_latency_ms": 150.0,
            "total_latency_ms": 400.0,
            "fallback_reason": "TimeoutError: candidate deadline",
        },
    )

    summary = summarize_shadow(journal, "gen-a", now=now)

    assert summary["query_samples"] == 1
    assert summary["duplicate_query_samples"] == 1
    assert summary["query_request_hash_conflicts"] == 0
    assert summary["candidate_queries"] == 1
    assert summary["candidate_fallbacks"] == 1
    assert summary["candidate_p95_ms"] == 400.0


def test_sparse_daily_health_probe_cannot_pass_shadow(tmp_path) -> None:
    journal = tmp_path / "shadow.jsonl"
    now = datetime(2026, 8, 25, 12, tzinfo=timezone.utc)
    for days_ago in range(7):
        timestamp = now - timedelta(days=days_ago)
        append_shadow_event(
            journal,
            {
                "kind": "health",
                "generation_id": "gen-a",
                "timestamp": timestamp.isoformat(),
                "available": True,
            },
        )
        append_shadow_event(
            journal,
            {
                "kind": "incremental",
                "generation_id": "gen-a",
                "timestamp": timestamp.isoformat(),
                "success": True,
            },
        )
        append_shadow_event(
            journal,
            {
                "kind": "source_link",
                "generation_id": "gen-a",
                "timestamp": timestamp.isoformat(),
                "accurate": True,
            },
        )

    summary = summarize_shadow(journal, "gen-a", now=now)

    assert summary["ready"] is False
    assert "health_samples_below_168" in summary["failure_reasons"]


def test_concentrated_health_probes_cannot_manufacture_hourly_coverage(tmp_path) -> None:
    journal = tmp_path / "shadow.jsonl"
    now = datetime(2026, 8, 25, 12, tzinfo=timezone.utc)
    for index in range(168):
        append_shadow_event(
            journal,
            {
                "kind": "health",
                "generation_id": "gen-a",
                "timestamp": (now - timedelta(seconds=index)).isoformat(),
                "available": True,
            },
        )
    for days_ago in range(7):
        timestamp = now - timedelta(days=days_ago)
        append_shadow_event(
            journal,
            {
                "kind": "incremental",
                "generation_id": "gen-a",
                "timestamp": timestamp.isoformat(),
                "success": True,
            },
        )
        append_shadow_event(
            journal,
            {
                "kind": "source_link",
                "generation_id": "gen-a",
                "timestamp": timestamp.isoformat(),
                "accurate": True,
            },
        )

    summary = summarize_shadow(journal, "gen-a", now=now)

    assert summary["health_events"] == 168
    assert summary["health_samples"] == 2
    assert summary["ready"] is False
    assert "health_samples_below_168" in summary["failure_reasons"]


def test_legacy_cursor_and_noop_events_never_count_as_refresh_evidence(
    tmp_path,
) -> None:
    journal = tmp_path / "shadow.jsonl"
    now = datetime(2026, 8, 25, 12, tzinfo=timezone.utc)
    candidate_context = _generation_context("gen-a", "candidate")
    for index in range(7):
        timestamp = now - timedelta(minutes=index)
        append_shadow_event(
            journal,
            {
                "kind": "incremental",
                "generation_id": "gen-a",
                "timestamp": timestamp.isoformat(),
                "success": True,
            },
        )
        append_shadow_event(
            journal,
            {
                "kind": "source_freshness",
                "generation_id": "gen-a",
                "timestamp": timestamp.isoformat(),
                "producer": "source-freshness.v1",
                "success": True,
                "noop": True,
            },
        )
    append_shadow_event(
        journal,
        _refresh_receipt(
            candidate_context,
            "gen-output-noop",
            "noop-output",
            timestamp=now,
            success=False,
            noop=True,
        ),
    )

    summary = summarize_shadow(journal, "gen-a", now=now)

    assert summary["incremental_consecutive_successes"] == 0
    assert summary["incremental_refresh_days"] == 0
    assert summary["legacy_incremental_events_ignored"] == 7
    assert summary["source_freshness_events_ignored"] == 7
    assert summary["noop_incremental_refresh_receipts_ignored"] == 1
    assert "authoritative_incremental_refreshes_below_7" in summary["failure_reasons"]
    assert (
        "legacy_incremental_events_are_not_refresh_receipts"
        in summary["failure_reasons"]
    )
    assert (
        "source_freshness_events_are_not_refresh_receipts"
        in summary["failure_reasons"]
    )
    assert (
        "noop_refresh_receipts_are_not_refresh_evidence"
        in summary["failure_reasons"]
    )


def test_same_day_refresh_failure_dominates_successful_receipts(tmp_path) -> None:
    journal = tmp_path / "shadow.jsonl"
    now = datetime(2026, 8, 25, 12, tzinfo=timezone.utc)
    candidate_context = _generation_context("gen-a", "candidate")
    for index, success in enumerate((True, False, True)):
        append_shadow_event(
            journal,
            _refresh_receipt(
                candidate_context,
                f"gen-output-{index}",
                f"output-{index}",
                timestamp=now,
                success=success,
                include_output_context=success,
            ),
        )
        append_shadow_event(
            journal,
            {
                "kind": "source_link",
                "generation_id": "gen-a",
                "timestamp": now.isoformat(),
                "accurate": success,
            },
        )

    summary = summarize_shadow(journal, "gen-a", now=now)

    assert summary["incremental_consecutive_successes"] == 0
    assert summary["incremental_refresh_days"] == 1
    assert summary["incremental_refresh_failed_days"] == 1
    assert "failed_incremental_refresh_receipts_observed" in summary["failure_reasons"]
    assert summary["source_link_audits"] == 1
    assert summary["bad_source_links"] == 1


def test_duplicate_health_probe_cannot_dilute_an_hourly_outage(tmp_path) -> None:
    journal = tmp_path / "shadow.jsonl"
    now = datetime(2026, 8, 25, 12, tzinfo=timezone.utc)
    for hours_ago in range(168):
        append_shadow_event(
            journal,
            {
                "kind": "health",
                "generation_id": "gen-a",
                "timestamp": (now - timedelta(hours=hours_ago)).isoformat(),
                "available": True,
            },
        )
    append_shadow_event(
        journal,
        {
            "kind": "health",
            "generation_id": "gen-a",
            "timestamp": (now - timedelta(minutes=1)).isoformat(),
            "available": False,
        },
    )

    summary = summarize_shadow(journal, "gen-a", now=now)

    assert summary["health_events"] == 169
    assert summary["health_samples"] == 168
    assert summary["availability"] == pytest.approx(167 / 168)
    assert "availability_below_0.995" in summary["failure_reasons"]


def test_guard_shadow_metrics_use_recent_reranker_and_three_real_windows(
    tmp_path,
) -> None:
    journal = tmp_path / "shadow.jsonl"
    now = datetime(2026, 8, 25, 12, tzinfo=timezone.utc)
    for index, minutes_ago in enumerate((25, 15, 5)):
        append_shadow_event(
            journal,
            {
                "kind": "query",
                "generation_id": "gen-a",
                "timestamp": (now - timedelta(minutes=minutes_ago)).isoformat(),
                "route": "candidate",
                "latency_ms": 9000.0 if index < 2 else 200.0,
                "reranker_attempted": True,
                "reranker_error": index == 2,
                "request_hash": _request_hash(index),
            },
        )
    append_shadow_event(
        journal,
        {
            "kind": "source_link",
            "generation_id": "gen-a",
            "timestamp": now.isoformat(),
            "accurate": True,
        },
    )
    for index in range(6):
        append_shadow_event(
            journal,
            {
                "kind": "source_link",
                "generation_id": "gen-a",
                "timestamp": (now - timedelta(minutes=index + 1)).isoformat(),
                "accurate": True,
            },
        )

    result = guard_shadow_metrics(journal, "gen-a", now=now)

    assert [window["sample_count"] for window in result["p95_windows"]] == [1, 1, 1]
    assert [window["p95_ms"] for window in result["p95_windows"]] == [9000.0, 9000.0, 200.0]
    assert result["reranker"]["request_count"] == 1
    assert result["reranker"]["error_count"] == 1
    assert result["reranker"]["error_rate"] == 1.0
    assert result["source_links"]["audit_count"] == 1


def test_guard_p95_uses_total_canary_latency_including_fallback(tmp_path) -> None:
    journal = tmp_path / "shadow.jsonl"
    now = datetime(2026, 8, 25, 12, tzinfo=timezone.utc)
    for index, (minutes_ago, total_ms) in enumerate(((25, 111.0), (15, 222.0), (5, 333.0))):
        append_shadow_event(
            journal,
            {
                "kind": "query",
                "generation_id": "gen-a",
                "timestamp": (now - timedelta(minutes=minutes_ago)).isoformat(),
                "route": "baseline" if index == 2 else "candidate",
                "request_hash": _request_hash(index),
                "latency_ms": total_ms,
                "selected_candidate": True,
                "served_route": "baseline" if index == 2 else "candidate",
                "candidate_latency_ms": 50.0,
                "total_latency_ms": total_ms,
                "fallback_reason": "TimeoutError: candidate deadline" if index == 2 else None,
                "reranker_attempted": True,
                "reranker_error": False,
            },
        )
    append_shadow_event(
        journal,
        {
            "kind": "source_link",
            "generation_id": "gen-a",
            "timestamp": now.isoformat(),
            "accurate": True,
        },
    )

    result = guard_shadow_metrics(journal, "gen-a", now=now)

    assert [window["p95_ms"] for window in result["p95_windows"]] == [
        111.0,
        222.0,
        333.0,
    ]


def test_guard_shadow_metrics_reject_missing_observation_windows(tmp_path) -> None:
    journal = tmp_path / "shadow.jsonl"
    now = datetime(2026, 8, 25, 12, tzinfo=timezone.utc)
    append_shadow_event(
        journal,
        {
            "kind": "source_link",
            "generation_id": "gen-a",
            "timestamp": now.isoformat(),
            "accurate": True,
        },
    )

    with pytest.raises(ValueError, match="p95 window"):
        guard_shadow_metrics(journal, "gen-a", now=now)


def test_guard_shadow_metrics_rejects_ambiguous_reranker_denominator(tmp_path) -> None:
    journal = tmp_path / "shadow.jsonl"
    now = datetime(2026, 8, 25, 12, tzinfo=timezone.utc)
    for index, minutes_ago in enumerate((25, 15, 5)):
        append_shadow_event(
            journal,
            {
                "kind": "query",
                "generation_id": "gen-a",
                "timestamp": (now - timedelta(minutes=minutes_ago)).isoformat(),
                "route": "candidate",
                "latency_ms": 100.0,
                "reranker_error": False,
                "request_hash": _request_hash(index),
            },
        )
    append_shadow_event(
        journal,
        {
            "kind": "source_link",
            "generation_id": "gen-a",
            "timestamp": now.isoformat(),
            "accurate": True,
        },
    )

    with pytest.raises(ValueError, match="reranker_attempted"):
        guard_shadow_metrics(journal, "gen-a", now=now)
