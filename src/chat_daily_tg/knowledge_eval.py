"""Offline gold-set evaluation and release gates for KnowledgeIndex."""

from __future__ import annotations

import json
import math
import os
import platform
import statistics
import sys
import time
import unicodedata
import uuid
from pathlib import Path
from typing import Any, Sequence

from chat_daily_tg.knowledge_index import (
    GenerationReader,
    sha256_bytes,
    sha256_file,
    sha256_text,
    utc_now,
    verify_generation,
)


QUALITY_THRESHOLDS = {
    "recall_at_50_min": 0.95,
    "source_recall_at_50_min": 0.90,
    "exact_recall_min": 1.00,
    "reranker_ndcg_relative_gain_min": 0.08,
    "precision_at_5_min": 0.80,
    "citation_accuracy_min": 1.00,
    "lexical_dense_p95_ms_max": 350.0,
    "e2e_regression_max": 0.20,
    "absolute_timeout_ms": 8000.0,
    "embedding_coverage_min": 0.995,
    "reranker_error_rate_max": 0.02,
}

GOLD_KIND_GROUPS = {
    "exact": frozenset({"exact", "url", "content_id", "bvid", "youtube_id", "model"}),
    "semantic": frozenset({"semantic", "semantic_paraphrase", "paraphrase"}),
    "cross_source": frozenset({"cross_source", "cross_source_same_event", "same_event"}),
    "long_form": frozenset({"long_form", "longform", "article", "transcript"}),
    "image": frozenset({"image", "ocr", "image_ocr"}),
}
GOLD_KIND_MINIMUMS = {
    "exact": 40,
    "semantic": 60,
    "cross_source": 40,
    "long_form": 30,
}

EVALUATION_SCHEMA = "chatdaily-knowledge-evaluation.v1"
BASELINE_PROVENANCE_SCHEMA = "chatdaily-knowledge-baseline-provenance.v1"
ARTIFACT_IDENTITY_SCHEMA = "chatdaily-knowledge-generation-artifact.v1"
STABLE_ARTIFACT_IDENTITY_FIELDS = (
    "generation_id",
    "manifest_hash",
    "catalog_hash",
    "vectors_hash",
)


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"generation manifest has duplicate JSON key: {key}")
        result[key] = value
    return result


def generation_artifact_identity(
    generation_dir: Path,
    *,
    expected_generation_id: str | None = None,
) -> dict[str, str]:
    """Return a live, content-addressed identity for one generation."""

    directory = generation_dir.expanduser().absolute()
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("generation artifact directory must be a regular directory")
    manifest_path = directory / "manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ValueError("generation artifact manifest must be a regular non-symlink file")
    try:
        manifest_bytes = manifest_path.read_bytes()
        manifest = json.loads(
            manifest_bytes.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("generation artifact manifest is invalid") from exc
    if not isinstance(manifest, dict):
        raise ValueError("generation artifact manifest must be an object")
    generation_id = manifest.get("generation_id")
    if (
        not isinstance(generation_id, str)
        or not generation_id.strip()
        or directory.name != generation_id
        or (expected_generation_id is not None and generation_id != expected_generation_id)
    ):
        raise ValueError("generation artifact identity has an invalid generation_id")
    identity: dict[str, str] = {
        "schema": ARTIFACT_IDENTITY_SCHEMA,
        "generation_id": generation_id,
        "manifest_sha256": sha256_bytes(manifest_bytes),
    }
    for field in ("manifest_hash", "catalog_hash", "vectors_hash"):
        value = manifest.get(field)
        if not _is_sha256(value):
            raise ValueError(f"generation artifact identity has an invalid {field}")
        identity[field] = value
    for filename, field in (
        ("catalog.sqlite", "catalog_hash"),
        ("vectors.f32", "vectors_hash"),
    ):
        artifact = directory / filename
        if artifact.is_symlink() or not artifact.is_file():
            raise ValueError(f"generation artifact {filename} must be a regular non-symlink file")
        if sha256_file(artifact) != identity[field]:
            raise ValueError(f"generation artifact {field} mismatch")
    if (
        manifest_path.is_symlink()
        or not manifest_path.is_file()
        or sha256_file(manifest_path) != identity["manifest_sha256"]
    ):
        raise ValueError("generation artifact manifest changed while being identified")
    return identity


def _validate_artifact_identity(value: Any, *, label: str) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != {
        "schema",
        "generation_id",
        "manifest_sha256",
        "manifest_hash",
        "catalog_hash",
        "vectors_hash",
    }:
        raise ValueError(f"{label} artifact identity is invalid")
    if value.get("schema") != ARTIFACT_IDENTITY_SCHEMA:
        raise ValueError(f"{label} artifact identity schema is invalid")
    generation_id = value.get("generation_id")
    if not isinstance(generation_id, str) or not generation_id.strip():
        raise ValueError(f"{label} artifact generation_id is invalid")
    for field in ("manifest_sha256", "manifest_hash", "catalog_hash", "vectors_hash"):
        if not _is_sha256(value.get(field)):
            raise ValueError(f"{label} artifact {field} is invalid")
    return dict(value)


def _stable_artifact_identity(identity: dict[str, str]) -> dict[str, str]:
    """Return fields that cannot change during a legitimate activation.

    ``manifest_sha256`` remains sealed audit evidence, but status and
    activation timestamps legitimately change the manifest file bytes when a
    ready generation becomes active.  The manifest's retrieval identity hash
    and its content-addressed catalog/vector hashes remain immutable.
    """

    return {field: identity[field] for field in STABLE_ARTIFACT_IDENTITY_FIELDS}


def _reader_artifact_identity(reader: GenerationReader) -> dict[str, str]:
    generation_id = reader.manifest.get("generation_id")
    if not isinstance(generation_id, str) or not generation_id:
        raise ValueError("evaluation reader generation_id is missing")
    identity = generation_artifact_identity(
        reader.generation_dir,
        expected_generation_id=generation_id,
    )
    for field in ("manifest_hash", "catalog_hash", "vectors_hash"):
        if reader.manifest.get(field) != identity[field]:
            raise ValueError(f"evaluation reader {field} does not match live manifest")
    return identity


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _normalized_query(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    return " ".join(normalized.split()).casefold()


def _evaluation_id(value: dict[str, Any], normalized_query: str) -> str:
    explicit = value.get("id")
    if explicit is None:
        return sha256_text(normalized_query)[:16]
    if not isinstance(explicit, str):
        raise ValueError("gold query id must be a string")
    result = unicodedata.normalize("NFKC", explicit).strip()
    if not result:
        raise ValueError("gold query id must not be empty")
    return result


def _optional_expected_identities(
    value: dict[str, Any],
    field: str,
    *,
    path: Path,
    line_number: int,
) -> list[str] | None:
    """Return one optional, canonical, duplicate-free gold identity list."""

    if field not in value:
        return None
    raw = value[field]
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"gold query has invalid {field} at {path}:{line_number}")
    normalized: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            raise ValueError(f"gold query has invalid {field} at {path}:{line_number}")
        identity = unicodedata.normalize("NFKC", item).strip()
        if not identity:
            raise ValueError(f"gold query has invalid {field} at {path}:{line_number}")
        normalized.append(identity)
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"gold query has duplicate {field} at {path}:{line_number}")
    return normalized


def _read_gold(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    query_lines: dict[str, int] = {}
    id_lines: dict[str, int] = {}
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        value = json.loads(line)
        if (
            not isinstance(value, dict)
            or not isinstance(value.get("query"), str)
            or not value["query"].strip()
        ):
            raise ValueError(f"invalid gold query at {path}:{line_number}")
        relevant = value.get("relevant_content_ids")
        if (
            not isinstance(relevant, list)
            or not relevant
            or any(not isinstance(item, str) or not item.strip() for item in relevant)
        ):
            raise ValueError(f"gold query has no relevant_content_ids at {path}:{line_number}")
        expected_refs = value.get("expected_source_refs")
        if (
            not isinstance(expected_refs, list)
            or not expected_refs
            or any(not isinstance(item, str) or not item.strip() for item in expected_refs)
        ):
            raise ValueError(f"gold query has no expected_source_refs at {path}:{line_number}")
        expected_links = value.get("expected_source_links")
        if not isinstance(expected_links, list) or any(
            not isinstance(item, dict)
            or not str(item.get("ledger_schema") or "").strip()
            or item.get("chat_id") is None
            or item.get("message_id") is None
            for item in expected_links
        ):
            raise ValueError(
                f"gold query has invalid expected_source_links at {path}:{line_number}"
            )
        if not isinstance(value.get("source_kind"), str) or not value["source_kind"].strip():
            raise ValueError(f"gold query has no source_kind at {path}:{line_number}")
        if not isinstance(value.get("kind"), str) or not value["kind"].strip():
            raise ValueError(f"gold query has no kind at {path}:{line_number}")
        expected_member_ids = _optional_expected_identities(
            value,
            "expected_member_ids",
            path=path,
            line_number=line_number,
        )
        expected_locators = _optional_expected_identities(
            value,
            "expected_locators",
            path=path,
            line_number=line_number,
        )
        normalized_query = _normalized_query(str(value["query"]))
        if not normalized_query:
            raise ValueError(f"invalid gold query at {path}:{line_number}")
        if normalized_query in query_lines:
            raise ValueError(
                f"duplicate normalized gold query at {path}:{line_number} "
                f"(first seen at line {query_lines[normalized_query]})"
            )
        try:
            evaluation_id = _evaluation_id(value, normalized_query)
        except ValueError as exc:
            raise ValueError(f"{exc} at {path}:{line_number}") from exc
        if evaluation_id in id_lines:
            raise ValueError(
                f"duplicate gold query id at {path}:{line_number} "
                f"(first seen at line {id_lines[evaluation_id]})"
            )
        query_lines[normalized_query] = line_number
        id_lines[evaluation_id] = line_number
        row = dict(value)
        if expected_member_ids is not None:
            row["expected_member_ids"] = expected_member_ids
        if expected_locators is not None:
            row["expected_locators"] = expected_locators
        row["_normalized_query"] = normalized_query
        row["_evaluation_id"] = evaluation_id
        rows.append(row)
    return rows


def _gold_structure(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    kind_counts = {name: 0 for name in (*GOLD_KIND_GROUPS, "other")}
    source_kind_counts: dict[str, int] = {}
    for row in rows:
        kind_counts[_kind_group(str(row["kind"]))] += 1
        source = str(row["source_kind"])
        source_kind_counts[source] = source_kind_counts.get(source, 0) + 1
    return {
        "query_count": len(rows),
        "kind_counts": kind_counts,
        "source_kind_counts": dict(sorted(source_kind_counts.items())),
    }


def _query_set_hash(rows: Sequence[dict[str, Any]]) -> str:
    identities = [
        {
            "id": str(row["_evaluation_id"]),
            "query": str(row["_normalized_query"]),
        }
        for row in rows
    ]
    return sha256_text(_canonical_json(identities))


def _required_source_kinds(reader: GenerationReader) -> list[str]:
    try:
        rows = reader.conn.execute(
            "SELECT DISTINCT source_kind FROM content_items "
            "WHERE active=1 ORDER BY source_kind"
        ).fetchall()
    except Exception as exc:
        raise ValueError("cannot derive required source kinds from candidate catalog") from exc
    result: list[str] = []
    for row in rows:
        value = row[0] if not isinstance(row, dict) else row.get("source_kind")
        if not isinstance(value, str) or not value.strip():
            raise ValueError("candidate catalog has an invalid active source kind")
        result.append(value)
    return result


def _host_fingerprint() -> str:
    identity = {
        "node": platform.node(),
        "system": platform.system(),
        "machine": platform.machine(),
    }
    return sha256_text(_canonical_json(identity))


def _report_hash(report: dict[str, Any]) -> str:
    unsigned = {key: value for key, value in report.items() if key != "report_hash"}
    return sha256_text(_canonical_json(unsigned))


def _seal_report(report: dict[str, Any]) -> dict[str, Any]:
    value = dict(report)
    value.pop("report_hash", None)
    value["report_hash"] = _report_hash(value)
    return value


def _kind_group(kind: str) -> str:
    normalized = kind.strip().lower().replace("-", "_")
    for group, aliases in GOLD_KIND_GROUPS.items():
        if normalized in aliases:
            return group
    return "other"


def _dcg(ids: Sequence[str], relevant: set[str], limit: int = 5) -> float:
    score = 0.0
    for rank, content_id in enumerate(ids[:limit], 1):
        if content_id in relevant:
            score += 1.0 / math.log2(rank + 1)
    return score


def _ndcg(ids: Sequence[str], relevant: set[str], limit: int = 5) -> float:
    ideal = sum(1.0 / math.log2(rank + 1) for rank in range(1, min(limit, len(relevant)) + 1))
    return _dcg(ids, relevant, limit) / ideal if ideal else 0.0


def _percentile(values: Sequence[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(quantile * len(ordered)) - 1))
    return float(ordered[index])


def _link_identity(value: dict[str, Any]) -> tuple[str, int, int | None, int, int | None]:
    return (
        str(value["ledger_schema"]),
        int(value["chat_id"]),
        int(value["thread_id"]) if value.get("thread_id") is not None else None,
        int(value["message_id"]),
        int(value["source_message_id"]) if value.get("source_message_id") is not None else None,
    )


def _load_frozen_gold(path: Path) -> tuple[list[dict[str, Any]], str]:
    before = path.read_text(encoding="utf-8")
    rows = _read_gold(path)
    after = path.read_text(encoding="utf-8")
    if before != after:
        raise ValueError("frozen gold set changed during evaluation")
    return rows, sha256_text(before)


def _evaluate_rows(
    reader: GenerationReader,
    gold_path: Path,
    rows: Sequence[dict[str, Any]],
    *,
    gold_set_hash: str,
    required_source_kinds: Sequence[str],
    release_evaluation: bool,
    baseline_e2e_p95_ms: float | None,
    baseline_provenance_valid: bool,
) -> dict[str, Any]:
    artifact_identity = _reader_artifact_identity(reader)
    structure = _gold_structure(rows)
    query_set_hash = _query_set_hash(rows)
    results: list[dict[str, Any]] = []
    recall_values: list[float] = []
    precision_values: list[float] = []
    rrf_ndcg_values: list[float] = []
    rerank_ndcg_values: list[float] = []
    exact_values: list[float] = []
    citation_values: list[float] = []
    source_recalls: dict[str, list[float]] = {}
    stratum_ndcg: dict[str, list[float]] = {}
    e2e_ms: list[float] = []
    lookup_ms: list[float] = []

    for case in rows:
        query = str(case["query"])
        relevant = {str(value) for value in case["relevant_content_ids"]}
        rrf = reader.search(query, top_k=50, use_reranker=False)
        started = time.perf_counter()
        reranked = reader.search(query, top_k=5, use_reranker=True)
        elapsed = (time.perf_counter() - started) * 1000
        if not math.isfinite(elapsed) or elapsed < 0.0:
            raise ValueError("knowledge evaluation produced an invalid E2E duration")
        # Preserve the raw finite float. Rounding here could turn an actual
        # 8000.001ms violation into 8000.00ms and incorrectly pass the gate.
        rrf_ids = [hit["content_id"] for hit in rrf["hits"]]
        reranked_ids = [hit["content_id"] for hit in reranked["hits"]]
        recall = len(relevant.intersection(rrf_ids)) / len(relevant)
        # Precision@5 must penalize under-filled result sets. Dividing by the
        # number returned can otherwise make one relevant hit look perfect.
        precision = len(relevant.intersection(reranked_ids[:5])) / 5.0
        rrf_ndcg = _ndcg(rrf_ids, relevant)
        rerank_ndcg = _ndcg(reranked_ids, relevant)
        recall_values.append(recall)
        precision_values.append(precision)
        rrf_ndcg_values.append(rrf_ndcg)
        rerank_ndcg_values.append(rerank_ndcg)
        source = str(case["source_kind"])
        source_recalls.setdefault(source, []).append(recall)
        stratum_ndcg.setdefault(source, []).append(rerank_ndcg)
        e2e_ms.append(elapsed)
        timing = reranked["timing_ms"]
        if "lookup" in timing:
            lookup_ms.append(float(timing["lookup"]))
        elif "dense_lookup" in timing:
            lookup_ms.append(
                float(timing.get("exact", 0.0))
                + float(timing.get("lexical", 0.0))
                + float(timing.get("dense_lookup", 0.0))
                + float(timing.get("rrf", 0.0))
            )
        else:
            # Compatibility for an older reader report. Its dense field includes
            # model inference, so this fallback is intentionally conservative.
            lookup_ms.append(float(timing["total"]))

        kind = str(case["kind"])
        if _kind_group(kind) == "exact":
            # Exact recall is intentionally channel-specific.  A relevant
            # identity recovered only by FTS or dense retrieval is useful for
            # overall Recall@50, but it must not hide a broken exact_terms
            # index or matcher.
            exact_channel_ids = {
                str(hit["content_id"])
                for hit in rrf["hits"]
                if "exact" in hit.get("channels", ())
            }
            exact_matched = sorted(relevant.intersection(exact_channel_ids))
            exact_values.append(len(exact_matched) / len(relevant))
        else:
            exact_matched = []
        relevant_hits = [
            hit for hit in reranked["hits"] if str(hit["content_id"]) in relevant
        ]
        expected_refs = {str(value) for value in case["expected_source_refs"]}
        actual_refs = {
            str(hit["source_ref"]) for hit in relevant_hits
        }
        expected_links = {_link_identity(value) for value in case["expected_source_links"]}
        actual_link_rows = [
            link
            for hit in relevant_hits
            for link in hit.get("source_links", [])
        ]
        actual_links = {_link_identity(value) for value in actual_link_rows}
        expected_member_ids = case.get("expected_member_ids")
        actual_member_ids: set[str] = set()
        member_ids_valid = True
        if expected_member_ids is not None:
            for hit in relevant_hits:
                raw_member_ids = hit.get("member_ids")
                if not isinstance(raw_member_ids, (list, tuple)) or any(
                    not isinstance(value, str) or not value.strip()
                    for value in raw_member_ids
                ):
                    member_ids_valid = False
                    break
                actual_member_ids.update(str(value) for value in raw_member_ids)
        member_ids_ok = expected_member_ids is None or (
            member_ids_valid and set(expected_member_ids) == actual_member_ids
        )
        expected_locators = case.get("expected_locators")
        actual_locators: set[str] = set()
        locators_valid = True
        if expected_locators is not None:
            for hit in relevant_hits:
                locator = hit.get("locator")
                if not isinstance(locator, str) or not locator.strip():
                    locators_valid = False
                    break
                actual_locators.add(locator)
        locators_ok = expected_locators is None or (
            locators_valid and set(expected_locators) == actual_locators
        )
        citation_ok = (
            expected_refs.issubset(actual_refs)
            and expected_links == actual_links
            and all(link.get("confirmed") in (True, 1) for link in actual_link_rows)
            and member_ids_ok
            and locators_ok
        )
        citation_values.append(float(citation_ok))
        result = {
            "id": str(case["_evaluation_id"]),
            "query": query,
            "kind": kind,
            "source_kind": source,
            "relevant": sorted(relevant),
            "rrf_top": rrf_ids[:5],
            "exact_matched": exact_matched,
            "reranked_top": reranked_ids,
            "recall_at_50": recall,
            "precision_at_5": precision,
            "rrf_ndcg_at_5": rrf_ndcg,
            "rerank_ndcg_at_5": rerank_ndcg,
            "e2e_ms": elapsed,
            "citation_ok": citation_ok,
            "degraded_reasons": reranked["degraded_reasons"],
        }
        if expected_member_ids is not None:
            result["citation_member_ids"] = sorted(actual_member_ids)
        if expected_locators is not None:
            result["citation_locators"] = sorted(actual_locators)
        results.append(result)

    recall = statistics.fmean(recall_values) if recall_values else 0.0
    precision = statistics.fmean(precision_values) if precision_values else 0.0
    rrf_ndcg = statistics.fmean(rrf_ndcg_values) if rrf_ndcg_values else 0.0
    rerank_ndcg = statistics.fmean(rerank_ndcg_values) if rerank_ndcg_values else 0.0
    relative_gain = (rerank_ndcg - rrf_ndcg) / rrf_ndcg if rrf_ndcg else 0.0
    source_metrics = {
        name: statistics.fmean(values) for name, values in sorted(source_recalls.items())
    }
    stratum_ndcg_metrics = {
        name: statistics.fmean(values) for name, values in sorted(stratum_ndcg.items())
    }
    full_verification = verify_generation(reader.generation_dir, full=True)
    if _reader_artifact_identity(reader) != artifact_identity:
        raise ValueError("generation artifact changed during evaluation")
    verification_stats = full_verification.get("stats") or {}
    reranker_error_count = sum(
        1
        for row in results
        if any(str(reason).startswith("rerank_failed:") for reason in row["degraded_reasons"])
    )
    metrics = {
        "query_count": len(rows),
        "kind_counts": structure["kind_counts"],
        "source_kind_counts": structure["source_kind_counts"],
        "required_source_kinds": list(required_source_kinds),
        "recall_at_50": recall,
        "source_recall_at_50": source_metrics,
        "stratum_ndcg_at_5": stratum_ndcg_metrics,
        "exact_recall": statistics.fmean(exact_values) if exact_values else None,
        "rrf_ndcg_at_5": rrf_ndcg,
        "reranker_ndcg_at_5": rerank_ndcg,
        "reranker_ndcg_relative_gain": relative_gain,
        "precision_at_5": precision,
        "citation_accuracy": statistics.fmean(citation_values) if citation_values else None,
        "lookup_p50_ms": _percentile(lookup_ms, 0.50),
        "lookup_p95_ms": _percentile(lookup_ms, 0.95),
        "e2e_p50_ms": _percentile(e2e_ms, 0.50),
        "e2e_p95_ms": _percentile(e2e_ms, 0.95),
        "max_e2e_ms": max(e2e_ms, default=0.0),
        "absolute_timeout_violation_count": sum(
            value > QUALITY_THRESHOLDS["absolute_timeout_ms"] for value in e2e_ms
        ),
        "baseline_e2e_p95_ms": baseline_e2e_p95_ms,
        "release_evaluation": release_evaluation,
        "baseline_provenance_valid": baseline_provenance_valid,
        "generation_valid": bool(full_verification.get("ok")),
        "embedding_coverage": float(verification_stats.get("coverage", 0.0)),
        "orphan_vectors": int(verification_stats.get("orphans", 0)),
        "nonfinite_vectors": "nonfinite_vectors" in full_verification.get("errors", []),
        "reranker_error_rate": reranker_error_count / len(rows) if rows else 0.0,
    }
    gates = quality_gate_reasons(metrics)
    return _seal_report(
        {
            "schema": EVALUATION_SCHEMA,
            "generation_id": reader.manifest["generation_id"],
            "artifact_identity": artifact_identity,
            "gold_set": str(gold_path.expanduser().absolute()),
            "gold_set_hash": gold_set_hash,
            "query_set_hash": query_set_hash,
            "gold_structure": structure,
            "required_source_kinds": list(required_source_kinds),
            "created_at": utc_now(),
            "thresholds": QUALITY_THRESHOLDS,
            "metrics": metrics,
            "baseline_generation_id": None,
            "host_fingerprint": None,
            "measurement_run_id": None,
            "baseline_provenance": None,
            "passed": not gates,
            "failure_reasons": gates,
            "results": results,
        }
    )


def evaluate_gold_set(
    reader: GenerationReader,
    gold_path: Path,
    *,
    allow_small: bool = False,
    baseline_e2e_p95_ms: float | None = None,
) -> dict[str, Any]:
    """Evaluate one generation; formal release still requires a paired run.

    A caller-supplied p95 scalar is retained only for an ``allow_small``
    diagnostic.  It is never accepted as formal release provenance.
    """

    rows, gold_set_hash = _load_frozen_gold(gold_path)
    if len(rows) < 200 and not allow_small:
        raise ValueError(f"release evaluation requires at least 200 queries, got {len(rows)}")
    diagnostic_baseline = baseline_e2e_p95_ms if allow_small else None
    report = _evaluate_rows(
        reader,
        gold_path,
        rows,
        gold_set_hash=gold_set_hash,
        required_source_kinds=_required_source_kinds(reader),
        release_evaluation=not allow_small,
        baseline_e2e_p95_ms=diagnostic_baseline,
        baseline_provenance_valid=False,
    )
    if baseline_e2e_p95_ms is not None and not allow_small:
        report["diagnostic_baseline_e2e_p95_ms"] = float(baseline_e2e_p95_ms)
        report = _seal_report(report)
    return report


def evaluate_release_pair(
    candidate_reader: GenerationReader,
    baseline_reader: GenerationReader,
    gold_path: Path,
    *,
    allow_small: bool = False,
) -> dict[str, Any]:
    """Measure candidate and baseline in one process over one frozen order."""

    rows, gold_set_hash = _load_frozen_gold(gold_path)
    if len(rows) < 200 and not allow_small:
        raise ValueError(f"release evaluation requires at least 200 queries, got {len(rows)}")
    candidate_generation = str(candidate_reader.manifest.get("generation_id") or "")
    baseline_generation = str(baseline_reader.manifest.get("generation_id") or "")
    if not candidate_generation or not baseline_generation:
        raise ValueError("paired evaluation requires candidate and baseline generation ids")
    if candidate_generation == baseline_generation:
        raise ValueError("paired evaluation requires different candidate and baseline generations")

    # Both loops consume the exact same in-memory rows in the same order and
    # process.  A fixed baseline-then-candidate order keeps the receipt
    # reproducible and explicit rather than hiding execution order.
    baseline = _evaluate_rows(
        baseline_reader,
        gold_path,
        rows,
        gold_set_hash=gold_set_hash,
        required_source_kinds=_required_source_kinds(baseline_reader),
        release_evaluation=False,
        baseline_e2e_p95_ms=None,
        baseline_provenance_valid=False,
    )
    baseline_p95 = float(baseline["metrics"]["e2e_p95_ms"])
    candidate = _evaluate_rows(
        candidate_reader,
        gold_path,
        rows,
        gold_set_hash=gold_set_hash,
        required_source_kinds=_required_source_kinds(candidate_reader),
        release_evaluation=not allow_small,
        baseline_e2e_p95_ms=baseline_p95,
        baseline_provenance_valid=True,
    )
    measurement_run_id = uuid.uuid4().hex
    host_fingerprint = _host_fingerprint()
    query_set_hash = _query_set_hash(rows)
    provenance = {
        "schema": BASELINE_PROVENANCE_SCHEMA,
        "same_run": True,
        "same_process": True,
        "measurement_order": "baseline_then_candidate_same_order",
        "measurement_run_id": measurement_run_id,
        "host_fingerprint": host_fingerprint,
        "candidate_generation_id": candidate_generation,
        "baseline_generation_id": baseline_generation,
        "candidate_artifact_identity": candidate["artifact_identity"],
        "baseline_artifact_identity": baseline["artifact_identity"],
        "candidate_query_set_hash": query_set_hash,
        "baseline_query_set_hash": query_set_hash,
        "candidate_query_count": len(rows),
        "baseline_query_count": len(rows),
        "candidate_e2e_p50_ms": float(candidate["metrics"]["e2e_p50_ms"]),
        "candidate_e2e_p95_ms": float(candidate["metrics"]["e2e_p95_ms"]),
        "baseline_e2e_p50_ms": float(baseline["metrics"]["e2e_p50_ms"]),
        "baseline_e2e_p95_ms": baseline_p95,
    }
    baseline.update(
        {
            "paired_candidate_generation_id": candidate_generation,
            "host_fingerprint": host_fingerprint,
            "measurement_run_id": measurement_run_id,
            "pair_role": "baseline",
        }
    )
    baseline = _seal_report(baseline)
    candidate.update(
        {
            "baseline_generation_id": baseline_generation,
            "host_fingerprint": host_fingerprint,
            "measurement_run_id": measurement_run_id,
            "baseline_provenance": provenance,
            # The guard producer extracts this exact baseline-side report so
            # both files come from the same process, frozen rows, and run.  It
            # remains embedded in the candidate receipt so extraction is
            # independently auditable and cannot substitute an older file.
            "paired_baseline_evaluation": baseline,
        }
    )
    if _reader_artifact_identity(candidate_reader) != candidate["artifact_identity"]:
        raise ValueError("candidate generation artifact changed during paired evaluation")
    if _reader_artifact_identity(baseline_reader) != baseline["artifact_identity"]:
        raise ValueError("baseline generation artifact changed during paired evaluation")
    return _seal_report(candidate)


def extract_paired_baseline_evaluation(paired_path: Path) -> dict[str, Any]:
    """Validate and return the baseline side of a paired evaluation receipt."""

    source = paired_path.expanduser().absolute()
    if source.is_symlink() or not source.is_file():
        raise ValueError("paired evaluation must be a regular non-symlink file")
    report = json.loads(source.read_text(encoding="utf-8"))
    if (
        not isinstance(report, dict)
        or report.get("schema") != EVALUATION_SCHEMA
        or not _is_sha256(report.get("report_hash"))
        or report["report_hash"] != _report_hash(report)
    ):
        raise ValueError("paired evaluation report is invalid")
    baseline = report.get("paired_baseline_evaluation")
    if (
        not isinstance(baseline, dict)
        or baseline.get("schema") != EVALUATION_SCHEMA
        or not _is_sha256(baseline.get("report_hash"))
        or baseline["report_hash"] != _report_hash(baseline)
    ):
        raise ValueError("paired baseline evaluation is invalid")
    provenance = report.get("baseline_provenance")
    if not isinstance(provenance, dict):
        raise ValueError("paired evaluation baseline provenance is missing")
    candidate_generation = report.get("generation_id")
    baseline_generation = report.get("baseline_generation_id")
    if (
        not isinstance(candidate_generation, str)
        or not candidate_generation
        or not isinstance(baseline_generation, str)
        or not baseline_generation
        or baseline.get("generation_id") != baseline_generation
        or baseline.get("paired_candidate_generation_id") != candidate_generation
    ):
        raise ValueError("paired evaluation generation binding is invalid")
    if (
        baseline.get("pair_role") != "baseline"
        or baseline.get("measurement_run_id") != report.get("measurement_run_id")
        or baseline.get("host_fingerprint") != report.get("host_fingerprint")
        or provenance.get("candidate_generation_id") != candidate_generation
        or provenance.get("baseline_generation_id") != baseline_generation
    ):
        raise ValueError("paired evaluation run binding is invalid")
    candidate_identity = _validate_artifact_identity(
        report.get("artifact_identity"), label="paired candidate"
    )
    baseline_identity = _validate_artifact_identity(
        baseline.get("artifact_identity"), label="paired baseline"
    )
    if (
        candidate_identity["generation_id"] != candidate_generation
        or baseline_identity["generation_id"] != baseline_generation
        or provenance.get("candidate_artifact_identity") != candidate_identity
        or provenance.get("baseline_artifact_identity") != baseline_identity
    ):
        raise ValueError("paired evaluation artifact binding is invalid")
    if (
        baseline.get("gold_set_hash") != report.get("gold_set_hash")
        or baseline.get("query_set_hash") != report.get("query_set_hash")
        or baseline.get("gold_structure") != report.get("gold_structure")
    ):
        raise ValueError("paired evaluation frozen gold binding is invalid")
    candidate_metrics = report.get("metrics")
    baseline_metrics = baseline.get("metrics")
    if (
        not isinstance(candidate_metrics, dict)
        or not isinstance(baseline_metrics, dict)
        or candidate_metrics.get("query_count") != baseline_metrics.get("query_count")
        or candidate_metrics.get("query_count") != provenance.get("candidate_query_count")
        or baseline_metrics.get("query_count") != provenance.get("baseline_query_count")
    ):
        raise ValueError("paired evaluation query count binding is invalid")
    return baseline


def _extract_paired_baseline_to_path(paired_path: Path, output: Path) -> None:
    baseline = extract_paired_baseline_evaluation(paired_path)
    destination = output.expanduser().absolute()
    if destination.exists() or destination.is_symlink():
        raise ValueError("paired baseline output already exists")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(baseline, ensure_ascii=False, indent=2) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _module_main(argv: Sequence[str]) -> int:
    if len(argv) != 3 or argv[0] != "extract-paired-baseline":
        raise SystemExit(
            "usage: python -m chat_daily_tg.knowledge_eval "
            "extract-paired-baseline PAIRED_EVALUATION OUTPUT"
        )
    _extract_paired_baseline_to_path(Path(argv[1]), Path(argv[2]))
    return 0


def evaluation_regression_metrics(
    current_report: dict[str, Any], baseline_report: dict[str, Any]
) -> dict[str, Any]:
    """Derive rollback deltas from two traceable frozen-gold reports.

    Both reports must describe the same gold-set hash and expose per-source
    nDCG.  Deltas are always baseline minus current, so a positive value is a
    regression and improvements remain negative rather than being hidden.
    """

    for label, report in (("current", current_report), ("baseline", baseline_report)):
        if (
            not isinstance(report, dict)
            or report.get("schema") != "chatdaily-knowledge-evaluation.v1"
        ):
            raise ValueError(f"{label} evaluation has an unknown schema")
        if not isinstance(report.get("generation_id"), str) or not report["generation_id"]:
            raise ValueError(f"{label} evaluation generation_id is required")
        if not isinstance(report.get("gold_set_hash"), str) or len(report["gold_set_hash"]) != 64:
            raise ValueError(f"{label} evaluation gold_set_hash is required")
        metrics = report.get("metrics")
        if not isinstance(metrics, dict):
            raise ValueError(f"{label} evaluation metrics must be an object")
        query_count = metrics.get("query_count")
        if isinstance(query_count, bool) or not isinstance(query_count, int) or query_count < 200:
            raise ValueError(f"{label} evaluation requires at least 200 queries")

    if current_report["gold_set_hash"] != baseline_report["gold_set_hash"]:
        raise ValueError("guard evaluations must use the same frozen gold set")

    def finite_metric(report: dict[str, Any], key: str, label: str) -> float:
        raw = report["metrics"].get(key)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise ValueError(f"{label} evaluation {key} must be numeric")
        value = float(raw)
        if not math.isfinite(value) or not 0.0 <= value <= 1.0:
            raise ValueError(f"{label} evaluation {key} must be finite in [0,1]")
        return value

    current_recall = finite_metric(current_report, "recall_at_50", "current")
    baseline_recall = finite_metric(baseline_report, "recall_at_50", "baseline")

    strata: dict[str, dict[str, float]] = {}
    raw_current = current_report["metrics"].get("stratum_ndcg_at_5")
    raw_baseline = baseline_report["metrics"].get("stratum_ndcg_at_5")
    if not isinstance(raw_current, dict) or not raw_current:
        raise ValueError("current evaluation stratum_ndcg_at_5 is required")
    if not isinstance(raw_baseline, dict) or not raw_baseline:
        raise ValueError("baseline evaluation stratum_ndcg_at_5 is required")
    if set(raw_current) != set(raw_baseline):
        raise ValueError("guard evaluation strata do not match")
    for name in sorted(raw_current):
        if not isinstance(name, str) or not name.strip():
            raise ValueError("guard evaluation stratum names must be non-empty strings")
        current_value = finite_metric(
            {"metrics": {"value": raw_current[name]}}, "value", f"current stratum {name}"
        )
        baseline_value = finite_metric(
            {"metrics": {"value": raw_baseline[name]}}, "value", f"baseline stratum {name}"
        )
        strata[name] = {
            "baseline": baseline_value,
            "current": current_value,
            "drop": baseline_value - current_value,
        }
    return {
        "current_generation_id": current_report["generation_id"],
        "baseline_generation_id": baseline_report["generation_id"],
        "gold_set_hash": current_report["gold_set_hash"],
        "current_query_count": int(current_report["metrics"]["query_count"]),
        "baseline_query_count": int(baseline_report["metrics"]["query_count"]),
        "recall_at_50": {
            "baseline": baseline_recall,
            "current": current_recall,
            "drop": baseline_recall - current_recall,
        },
        "stratum_ndcg_at_5": {
            "values": strata,
            "max_drop": max(value["drop"] for value in strata.values()),
        },
    }


def quality_gate_reasons(metrics: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    release_evaluation = metrics.get("release_evaluation") is True
    if int(metrics.get("query_count", 0)) < 200:
        reasons.append("gold_set_below_200")
    kind_counts = metrics.get("kind_counts")
    if not isinstance(kind_counts, dict):
        reasons.append("gold_set_kind_counts_missing")
    else:
        for group, minimum in GOLD_KIND_MINIMUMS.items():
            if int(kind_counts.get(group, 0)) < minimum:
                reasons.append(f"gold_set_{group}_below_{minimum}")
    required_source_kinds = metrics.get("required_source_kinds")
    source_kind_counts = metrics.get("source_kind_counts")
    if release_evaluation and (
        not isinstance(required_source_kinds, list) or not required_source_kinds
    ):
        reasons.append("required_source_kinds_missing")
    elif isinstance(required_source_kinds, list):
        if not isinstance(source_kind_counts, dict) or any(
            not isinstance(name, str)
            or not name.strip()
            or int(source_kind_counts.get(name, 0)) <= 0
            for name in required_source_kinds
        ):
            reasons.append("gold_set_missing_required_source_kinds")
    if float(metrics.get("recall_at_50", 0.0)) < QUALITY_THRESHOLDS["recall_at_50_min"]:
        reasons.append("recall_at_50_below_0.95")
    source = metrics.get("source_recall_at_50") or {}
    if not isinstance(source, dict) or not source:
        reasons.append("source_recall_at_50_missing")
    elif any(
        float(value) < QUALITY_THRESHOLDS["source_recall_at_50_min"] for value in source.values()
    ):
        reasons.append("source_recall_at_50_below_0.90")
    exact = metrics.get("exact_recall")
    if exact is None:
        reasons.append("exact_recall_missing")
    elif float(exact) < QUALITY_THRESHOLDS["exact_recall_min"]:
        reasons.append("exact_recall_below_1.00")
    if (
        float(metrics.get("reranker_ndcg_relative_gain", 0.0))
        < QUALITY_THRESHOLDS["reranker_ndcg_relative_gain_min"]
    ):
        reasons.append("reranker_ndcg_gain_below_8_percent")
    if float(metrics.get("precision_at_5", 0.0)) < QUALITY_THRESHOLDS["precision_at_5_min"]:
        reasons.append("precision_at_5_below_0.80")
    citation = metrics.get("citation_accuracy")
    if citation is None:
        reasons.append("citation_accuracy_missing")
    elif float(citation) < QUALITY_THRESHOLDS["citation_accuracy_min"]:
        reasons.append("citation_accuracy_below_1.00")
    if float(metrics.get("lookup_p95_ms", 0.0)) > QUALITY_THRESHOLDS["lexical_dense_p95_ms_max"]:
        reasons.append("lookup_p95_over_350ms")
    raw_max_e2e = metrics.get("max_e2e_ms")
    max_e2e_valid = (
        not isinstance(raw_max_e2e, bool)
        and isinstance(raw_max_e2e, (int, float))
        and math.isfinite(float(raw_max_e2e))
        and float(raw_max_e2e) >= 0.0
    )
    raw_violation_count = metrics.get("absolute_timeout_violation_count")
    violation_count_valid = (
        not isinstance(raw_violation_count, bool)
        and isinstance(raw_violation_count, int)
        and 0 <= raw_violation_count <= int(metrics.get("query_count", 0))
    )
    if not max_e2e_valid:
        reasons.append("max_e2e_ms_invalid")
    if not violation_count_valid:
        reasons.append("absolute_timeout_violation_count_invalid")
    if max_e2e_valid and violation_count_valid:
        max_exceeded = float(raw_max_e2e) > QUALITY_THRESHOLDS["absolute_timeout_ms"]
        if max_exceeded != (raw_violation_count > 0):
            reasons.append("absolute_timeout_metrics_inconsistent")
        if raw_violation_count > 0:
            reasons.append("absolute_timeout_violation")
    if float(metrics.get("e2e_p95_ms", 0.0)) > QUALITY_THRESHOLDS["absolute_timeout_ms"]:
        reasons.append("e2e_p95_over_8s")
    baseline = metrics.get("baseline_e2e_p95_ms")
    if baseline is None or float(baseline) <= 0.0:
        reasons.append("baseline_e2e_p95_ms_missing")
    elif float(metrics.get("e2e_p95_ms", 0.0)) > float(baseline) * (
        1.0 + QUALITY_THRESHOLDS["e2e_regression_max"]
    ):
        reasons.append("e2e_p95_regressed_over_20_percent")
    if release_evaluation and metrics.get("baseline_provenance_valid") is not True:
        reasons.append("baseline_provenance_missing")
    if metrics.get("generation_valid") is not True:
        reasons.append("generation_invalid")
    if float(metrics.get("embedding_coverage", 0.0)) < QUALITY_THRESHOLDS["embedding_coverage_min"]:
        reasons.append("embedding_coverage_below_0.995")
    if int(metrics.get("orphan_vectors", 0)):
        reasons.append("orphan_vectors")
    if metrics.get("nonfinite_vectors") is True:
        reasons.append("nonfinite_vectors")
    if (
        float(metrics.get("reranker_error_rate", 0.0))
        > QUALITY_THRESHOLDS["reranker_error_rate_max"]
    ):
        reasons.append("reranker_error_rate_over_0.02")
    return reasons


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _positive_finite(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"baseline provenance {label} must be numeric")
    result = float(value)
    if not math.isfinite(result) or result <= 0.0:
        raise ValueError(f"baseline provenance {label} must be finite and positive")
    return result


def _validate_baseline_provenance(
    report: dict[str, Any],
    *,
    generation_id: str,
    query_set_hash: str,
    query_count: int,
    candidate_identity: dict[str, str],
    baseline_identity: dict[str, str],
) -> None:
    provenance = report.get("baseline_provenance")
    if not isinstance(provenance, dict):
        raise ValueError("evaluation baseline provenance is missing")
    if provenance.get("schema") != BASELINE_PROVENANCE_SCHEMA:
        raise ValueError("evaluation baseline provenance schema is invalid")
    if provenance.get("same_run") is not True or provenance.get("same_process") is not True:
        raise ValueError("evaluation baseline was not measured in the same run and process")
    if provenance.get("measurement_order") != "baseline_then_candidate_same_order":
        raise ValueError("evaluation baseline measurement order is invalid")
    measurement_run_id = provenance.get("measurement_run_id")
    if (
        not isinstance(measurement_run_id, str)
        or len(measurement_run_id) != 32
        or any(character not in "0123456789abcdef" for character in measurement_run_id)
    ):
        raise ValueError("evaluation baseline measurement_run_id is missing")
    if report.get("measurement_run_id") != measurement_run_id:
        raise ValueError("evaluation baseline measurement_run_id mismatch")
    host_fingerprint = provenance.get("host_fingerprint")
    if (
        not _is_sha256(host_fingerprint)
        or report.get("host_fingerprint") != host_fingerprint
        or host_fingerprint != _host_fingerprint()
    ):
        raise ValueError("evaluation baseline host fingerprint is invalid")
    if provenance.get("candidate_generation_id") != generation_id:
        raise ValueError("evaluation baseline candidate generation mismatch")
    baseline_generation = provenance.get("baseline_generation_id")
    if (
        not isinstance(baseline_generation, str)
        or not baseline_generation.strip()
        or baseline_generation == generation_id
        or report.get("baseline_generation_id") != baseline_generation
    ):
        raise ValueError("evaluation baseline generation is invalid")
    if (
        provenance.get("candidate_artifact_identity") != candidate_identity
        or provenance.get("baseline_artifact_identity") != baseline_identity
    ):
        raise ValueError("evaluation baseline artifact identity mismatch")
    if (
        provenance.get("candidate_query_set_hash") != query_set_hash
        or provenance.get("baseline_query_set_hash") != query_set_hash
    ):
        raise ValueError("evaluation baseline query set hash mismatch")
    if (
        provenance.get("candidate_query_count") != query_count
        or provenance.get("baseline_query_count") != query_count
    ):
        raise ValueError("evaluation baseline query count mismatch")

    metrics = report.get("metrics")
    if not isinstance(metrics, dict):
        raise ValueError("evaluation metrics must be an object")
    candidate_p50 = _positive_finite(
        provenance.get("candidate_e2e_p50_ms"), "candidate_e2e_p50_ms"
    )
    candidate_p95 = _positive_finite(
        provenance.get("candidate_e2e_p95_ms"), "candidate_e2e_p95_ms"
    )
    baseline_p50 = _positive_finite(
        provenance.get("baseline_e2e_p50_ms"), "baseline_e2e_p50_ms"
    )
    baseline_p95 = _positive_finite(
        provenance.get("baseline_e2e_p95_ms"), "baseline_e2e_p95_ms"
    )
    if candidate_p50 != float(metrics.get("e2e_p50_ms", -1.0)):
        raise ValueError("evaluation baseline candidate p50 mismatch")
    if candidate_p95 != float(metrics.get("e2e_p95_ms", -1.0)):
        raise ValueError("evaluation baseline candidate p95 mismatch")
    if baseline_p95 != float(metrics.get("baseline_e2e_p95_ms", -1.0)):
        raise ValueError("evaluation baseline p95 mismatch")
    if baseline_p50 > baseline_p95 or candidate_p50 > candidate_p95:
        raise ValueError("evaluation baseline percentile ordering is invalid")
    if metrics.get("baseline_provenance_valid") is not True:
        raise ValueError("evaluation baseline provenance was not release-valid")


def validate_evaluation_for_activation(
    path: Path,
    generation_id: str,
    *,
    candidate_generation_dir: Path,
    baseline_generation_dir: Path,
) -> dict[str, Any]:
    report = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(report, dict) or report.get("schema") != EVALUATION_SCHEMA:
        raise ValueError("unknown knowledge evaluation schema")
    if not _is_sha256(report.get("report_hash")) or report["report_hash"] != _report_hash(report):
        raise ValueError("evaluation report hash mismatch")
    if report.get("generation_id") != generation_id:
        raise ValueError("evaluation generation does not match activation target")
    candidate_identity = _validate_artifact_identity(
        report.get("artifact_identity"), label="evaluation candidate"
    )
    live_candidate_identity = generation_artifact_identity(
        candidate_generation_dir,
        expected_generation_id=generation_id,
    )
    if _stable_artifact_identity(candidate_identity) != _stable_artifact_identity(
        live_candidate_identity
    ):
        raise ValueError("evaluation candidate artifact does not match live generation")
    baseline_generation_id = report.get("baseline_generation_id")
    if not isinstance(baseline_generation_id, str) or not baseline_generation_id:
        raise ValueError("evaluation baseline generation is invalid")
    live_baseline_identity = generation_artifact_identity(
        baseline_generation_dir,
        expected_generation_id=baseline_generation_id,
    )
    paired_baseline = report.get("paired_baseline_evaluation")
    if (
        not isinstance(paired_baseline, dict)
        or paired_baseline.get("schema") != EVALUATION_SCHEMA
        or not _is_sha256(paired_baseline.get("report_hash"))
        or paired_baseline["report_hash"] != _report_hash(paired_baseline)
    ):
        raise ValueError("paired baseline evaluation is invalid")
    sealed_baseline_identity = _validate_artifact_identity(
        paired_baseline.get("artifact_identity"), label="evaluation baseline"
    )
    if _stable_artifact_identity(sealed_baseline_identity) != _stable_artifact_identity(
        live_baseline_identity
    ):
        raise ValueError("evaluation baseline artifact does not match live generation")
    if (
        paired_baseline.get("generation_id") != baseline_generation_id
        or paired_baseline.get("paired_candidate_generation_id") != generation_id
        or paired_baseline.get("pair_role") != "baseline"
        or paired_baseline.get("measurement_run_id") != report.get("measurement_run_id")
        or paired_baseline.get("host_fingerprint") != report.get("host_fingerprint")
    ):
        raise ValueError("paired baseline evaluation binding is invalid")
    gold_path = Path(str(report.get("gold_set") or ""))
    if not gold_path.is_file():
        raise ValueError("frozen evaluation gold set is missing")
    rows, gold_set_hash = _load_frozen_gold(gold_path)
    if report.get("gold_set_hash") != gold_set_hash:
        raise ValueError("frozen evaluation gold set hash changed")
    structure = _gold_structure(rows)
    query_set_hash = _query_set_hash(rows)
    if report.get("query_set_hash") != query_set_hash:
        raise ValueError("frozen evaluation query set hash changed")
    if report.get("gold_structure") != structure:
        raise ValueError("evaluation gold structure does not match the frozen gold set")
    required_source_kinds = report.get("required_source_kinds")
    if (
        not isinstance(required_source_kinds, list)
        or not required_source_kinds
        or any(not isinstance(value, str) or not value.strip() for value in required_source_kinds)
        or required_source_kinds != sorted(set(required_source_kinds))
    ):
        raise ValueError("evaluation required source kinds are invalid")
    missing_sources = set(required_source_kinds) - set(structure["source_kind_counts"])
    if missing_sources:
        raise ValueError("frozen gold set does not cover every required source kind")
    metrics = report.get("metrics") if isinstance(report.get("metrics"), dict) else {}
    if metrics.get("query_count") != structure["query_count"]:
        raise ValueError("evaluation query count does not match the frozen gold set")
    if metrics.get("kind_counts") != structure["kind_counts"]:
        raise ValueError("evaluation kind counts do not match the frozen gold set")
    if metrics.get("source_kind_counts") != structure["source_kind_counts"]:
        raise ValueError("evaluation source kind counts do not match the frozen gold set")
    if metrics.get("required_source_kinds") != required_source_kinds:
        raise ValueError("evaluation required source kinds do not match metrics")
    if metrics.get("release_evaluation") is not True:
        raise ValueError("activation requires a formal release evaluation")
    results = report.get("results")
    if not isinstance(results, list) or len(results) != len(rows):
        raise ValueError("evaluation result count does not match the frozen gold set")
    result_e2e_ms: list[float] = []
    citation_values: list[float] = []
    for case, result in zip(rows, results, strict=True):
        if not isinstance(result, dict):
            raise ValueError("evaluation result row is invalid")
        if (
            result.get("id") != case["_evaluation_id"]
            or result.get("query") != str(case["query"])
            or result.get("kind") != str(case["kind"])
            or result.get("source_kind") != str(case["source_kind"])
        ):
            raise ValueError("evaluation result provenance does not match the frozen gold set")
        raw_e2e_ms = result.get("e2e_ms")
        if (
            isinstance(raw_e2e_ms, bool)
            or not isinstance(raw_e2e_ms, (int, float))
            or not math.isfinite(float(raw_e2e_ms))
            or float(raw_e2e_ms) < 0.0
        ):
            raise ValueError("evaluation result E2E duration is invalid")
        result_e2e_ms.append(float(raw_e2e_ms))
        if not isinstance(result.get("citation_ok"), bool):
            raise ValueError("evaluation result citation outcome is invalid")
        citation_values.append(float(result["citation_ok"]))
        for field, evidence_field in (
            ("expected_member_ids", "citation_member_ids"),
            ("expected_locators", "citation_locators"),
        ):
            if field not in case:
                continue
            evidence = result.get(evidence_field)
            if (
                not isinstance(evidence, list)
                or any(not isinstance(value, str) or not value.strip() for value in evidence)
                or evidence != sorted(set(evidence))
            ):
                raise ValueError(f"evaluation result {evidence_field} is invalid")
            if set(evidence) != set(case[field]) and result["citation_ok"] is True:
                raise ValueError("evaluation result citation chunk identity is invalid")
    computed_max_e2e_ms = max(result_e2e_ms, default=0.0)
    computed_timeout_violations = sum(
        value > QUALITY_THRESHOLDS["absolute_timeout_ms"] for value in result_e2e_ms
    )
    if metrics.get("max_e2e_ms") != computed_max_e2e_ms:
        raise ValueError("evaluation max E2E metric does not match result rows")
    if metrics.get("absolute_timeout_violation_count") != computed_timeout_violations:
        raise ValueError("evaluation timeout violation count does not match result rows")
    computed_citation_accuracy = statistics.fmean(citation_values) if citation_values else None
    if metrics.get("citation_accuracy") != computed_citation_accuracy:
        raise ValueError("evaluation citation accuracy does not match result rows")
    _validate_baseline_provenance(
        report,
        generation_id=generation_id,
        query_set_hash=query_set_hash,
        query_count=len(rows),
        candidate_identity=candidate_identity,
        baseline_identity=sealed_baseline_identity,
    )
    reasons = quality_gate_reasons(metrics)
    if report.get("failure_reasons") != reasons:
        raise ValueError("evaluation failure reasons do not match recomputed gates")
    if reasons or report.get("passed") is not True:
        raise ValueError(f"evaluation does not satisfy release gates: {reasons}")
    return report


if __name__ == "__main__":
    raise SystemExit(_module_main(sys.argv[1:]))
