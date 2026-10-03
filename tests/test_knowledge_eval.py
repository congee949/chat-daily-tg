from __future__ import annotations

import copy
import json
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

import pytest

from chat_daily_tg import knowledge_eval
from chat_daily_tg.knowledge_eval import (
    evaluate_gold_set,
    evaluate_release_pair,
    evaluation_regression_metrics,
    quality_gate_reasons,
    validate_evaluation_for_activation,
)
from chat_daily_tg.knowledge_index import sha256_text


def _passing_metrics() -> dict:
    return {
        "query_count": 200,
        "kind_counts": {
            "exact": 40,
            "semantic": 60,
            "cross_source": 40,
            "long_form": 30,
            "image": 0,
            "other": 30,
        },
        "recall_at_50": 0.96,
        "source_recall_at_50": {"archive": 0.91, "podcast": 0.93},
        "stratum_ndcg_at_5": {"archive": 0.80, "podcast": 0.85},
        "exact_recall": 1.0,
        "reranker_ndcg_relative_gain": 0.09,
        "precision_at_5": 0.81,
        "citation_accuracy": 1.0,
        "lookup_p95_ms": 300.0,
        "e2e_p95_ms": 7000.0,
        "max_e2e_ms": 7000.0,
        "absolute_timeout_violation_count": 0,
        "baseline_e2e_p95_ms": 6500.0,
        "generation_valid": True,
        "embedding_coverage": 1.0,
        "orphan_vectors": 0,
        "nonfinite_vectors": False,
        "reranker_error_rate": 0.0,
    }


def _gold_case(index: int, *, source_kind: str | None = None) -> dict[str, Any]:
    if index < 40:
        kind = "exact"
    elif index < 100:
        kind = "semantic"
    elif index < 140:
        kind = "cross_source"
    elif index < 170:
        kind = "long_form"
    else:
        kind = "other"
    return {
        "id": f"case-{index}",
        "query": f"query {index}",
        "relevant_content_ids": [f"content-{index}-{item}" for item in range(4)],
        "expected_source_refs": ["ref"],
        "expected_source_links": [],
        "source_kind": source_kind or ("archive" if index % 2 == 0 else "podcast"),
        "kind": kind,
    }


def _write_gold(path: Path, *, count: int = 200) -> Path:
    path.write_text(
        "".join(json.dumps(_gold_case(index), ensure_ascii=False) + "\n" for index in range(count)),
        encoding="utf-8",
    )
    return path


class _FakeReader:
    def __init__(
        self,
        generation_id: str,
        source_kinds: tuple[str, ...],
        *,
        generation_root: Path | None = None,
        artifact_salt: str = "original",
    ):
        self._temporary_directory = (
            tempfile.TemporaryDirectory() if generation_root is None else None
        )
        root = (
            Path(self._temporary_directory.name)
            if self._temporary_directory is not None
            else generation_root
        )
        assert root is not None
        self.generation_dir = root / generation_id
        self.generation_dir.mkdir(parents=True)
        catalog = self.generation_dir / "catalog.sqlite"
        vectors = self.generation_dir / "vectors.f32"
        catalog.write_bytes(f"catalog:{generation_id}:{artifact_salt}".encode())
        vectors.write_bytes(f"vectors:{generation_id}:{artifact_salt}".encode())
        self.manifest = {
            "generation_id": generation_id,
            "manifest_hash": sha256_text(f"manifest:{generation_id}:{artifact_salt}"),
            "catalog_hash": knowledge_eval.sha256_file(catalog),
            "vectors_hash": knowledge_eval.sha256_file(vectors),
            "status": "ready",
        }
        (self.generation_dir / "manifest.json").write_text(
            json.dumps(self.manifest, sort_keys=True),
            encoding="utf-8",
        )
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute("CREATE TABLE content_items(source_kind TEXT, active INTEGER)")
        self.conn.executemany(
            "INSERT INTO content_items VALUES(?,1)",
            [(source_kind,) for source_kind in source_kinds],
        )

    @staticmethod
    def _hit(content_id: str, *, channels: tuple[str, ...] = ("exact",)) -> dict[str, Any]:
        return {
            "content_id": content_id,
            "source_ref": "ref",
            "source_links": [],
            "channels": channels,
        }

    def search(self, query: str, *, top_k: int, use_reranker: bool) -> dict[str, Any]:
        index = int(query.rsplit(" ", 1)[1])
        relevant = [f"content-{index}-{item}" for item in range(4)]
        filler = [f"filler-{index}-{item}" for item in range(10)]
        if use_reranker:
            ids = [*relevant, filler[0]]
        else:
            ids = [filler[0], filler[1], filler[2], relevant[0], relevant[1], *relevant[2:]]
        return {
            "hits": [self._hit(content_id) for content_id in ids[:top_k]],
            "timing_ms": {"lookup": 10.0, "total": 12.0},
            "degraded_reasons": [],
        }


def _prepare_evaluation(monkeypatch) -> None:
    monkeypatch.setattr(
        knowledge_eval,
        "verify_generation",
        lambda *_args, **_kwargs: {
            "ok": True,
            "stats": {"coverage": 1.0, "orphans": 0},
            "errors": [],
        },
    )
    clock = 0.0

    def perf_counter() -> float:
        nonlocal clock
        clock += 0.001
        return clock

    monkeypatch.setattr(knowledge_eval.time, "perf_counter", perf_counter)


def _release_report(tmp_path: Path, monkeypatch, *, candidate_sources=None):
    _prepare_evaluation(monkeypatch)
    gold = _write_gold(tmp_path / "gold.jsonl")
    candidate = _FakeReader(
        "gen-candidate",
        tuple(candidate_sources or ("archive", "podcast")),
        generation_root=tmp_path / "generations",
    )
    baseline = _FakeReader(
        "gen-baseline",
        ("archive", "podcast"),
        generation_root=tmp_path / "generations",
    )
    report = evaluate_release_pair(candidate, baseline, gold)
    return gold, report, candidate, baseline


def _live_generation_kwargs(candidate: _FakeReader, baseline: _FakeReader) -> dict[str, Path]:
    return {
        "candidate_generation_dir": candidate.generation_dir,
        "baseline_generation_dir": baseline.generation_dir,
    }


def _write_report(path: Path, report: dict[str, Any]) -> Path:
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _reseal(report: dict[str, Any]) -> None:
    unsigned = {key: value for key, value in report.items() if key != "report_hash"}
    raw = json.dumps(
        unsigned,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    report["report_hash"] = sha256_text(raw)


def test_quality_gates_include_generation_integrity_and_coverage() -> None:
    metrics = _passing_metrics()

    assert quality_gate_reasons(metrics) == []

    metrics.update(
        {
            "generation_valid": False,
            "embedding_coverage": 0.994,
            "orphan_vectors": 1,
            "nonfinite_vectors": True,
            "reranker_error_rate": 0.021,
        }
    )
    reasons = quality_gate_reasons(metrics)

    assert "embedding_coverage_below_0.995" in reasons
    assert "generation_invalid" in reasons
    assert "nonfinite_vectors" in reasons
    assert "orphan_vectors" in reasons
    assert "reranker_error_rate_over_0.02" in reasons


def test_read_gold_rejects_nfkc_whitespace_casefold_duplicate_query(tmp_path) -> None:
    first = _gold_case(0)
    second = _gold_case(1)
    first["query"] = "Ａ   Query"
    second["query"] = "a query"
    gold = tmp_path / "duplicate-query.jsonl"
    gold.write_text(
        json.dumps(first, ensure_ascii=False) + "\n" + json.dumps(second) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="duplicate normalized gold query"):
        knowledge_eval._read_gold(gold)


@pytest.mark.parametrize("derived_collision", [False, True])
def test_read_gold_rejects_explicit_or_derived_duplicate_id(
    tmp_path, derived_collision: bool
) -> None:
    first = _gold_case(0)
    second = _gold_case(1)
    if derived_collision:
        first.pop("id")
        second["id"] = sha256_text(knowledge_eval._normalized_query(first["query"]))[:16]
    else:
        second["id"] = first["id"]
    gold = tmp_path / "duplicate-id.jsonl"
    gold.write_text(json.dumps(first) + "\n" + json.dumps(second) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="duplicate gold query id"):
        knowledge_eval._read_gold(gold)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("expected_member_ids", None, "invalid expected_member_ids"),
        ("expected_member_ids", [], "invalid expected_member_ids"),
        ("expected_member_ids", [""], "invalid expected_member_ids"),
        ("expected_member_ids", [1], "invalid expected_member_ids"),
        ("expected_member_ids", ["member-a", "member-a"], "duplicate expected_member_ids"),
        ("expected_locators", None, "invalid expected_locators"),
        ("expected_locators", [], "invalid expected_locators"),
        ("expected_locators", [""], "invalid expected_locators"),
        ("expected_locators", [1], "invalid expected_locators"),
        ("expected_locators", ["part-a", "part-a"], "duplicate expected_locators"),
    ],
)
def test_read_gold_rejects_invalid_optional_chunk_identities(
    tmp_path, field: str, value: Any, message: str
) -> None:
    case = _gold_case(0)
    case[field] = value
    gold = tmp_path / f"invalid-{field}.jsonl"
    gold.write_text(json.dumps(case) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        knowledge_eval._read_gold(gold)


def test_citation_binds_expected_members_and_locator_to_relevant_hit(
    tmp_path, monkeypatch
) -> None:
    _prepare_evaluation(monkeypatch)
    case = _gold_case(0)
    case["expected_member_ids"] = ["member-a", "member-b"]
    case["expected_locators"] = ["conversation/window-a"]
    gold = tmp_path / "chunk-citation.jsonl"
    gold.write_text(json.dumps(case) + "\n", encoding="utf-8")

    class ChunkAwareReader(_FakeReader):
        @staticmethod
        def _hit(content_id: str, *, channels=("exact",)) -> dict[str, Any]:
            hit = _FakeReader._hit(content_id, channels=channels)
            hit.update(
                {
                    "member_ids": ("member-a", "member-b"),
                    "locator": "conversation/window-a",
                }
            )
            return hit

    report = evaluate_gold_set(
        ChunkAwareReader("gen-candidate", ("archive",)),
        gold,
        allow_small=True,
    )

    assert report["results"][0]["citation_ok"] is True
    assert report["results"][0]["citation_member_ids"] == ["member-a", "member-b"]
    assert report["results"][0]["citation_locators"] == ["conversation/window-a"]


@pytest.mark.parametrize(
    ("actual_members", "actual_locator"),
    [
        (("member-a", "member-c"), "conversation/window-a"),
        (("member-a", "member-b"), "conversation/window-b"),
    ],
)
def test_citation_rejects_content_match_from_wrong_bundle_or_fragment(
    tmp_path, monkeypatch, actual_members: tuple[str, ...], actual_locator: str
) -> None:
    _prepare_evaluation(monkeypatch)
    case = _gold_case(0)
    case["expected_member_ids"] = ["member-a", "member-b"]
    case["expected_locators"] = ["conversation/window-a"]
    gold = tmp_path / "wrong-chunk-citation.jsonl"
    gold.write_text(json.dumps(case) + "\n", encoding="utf-8")

    class WrongChunkReader(_FakeReader):
        @staticmethod
        def _hit(content_id: str, *, channels=("exact",)) -> dict[str, Any]:
            hit = _FakeReader._hit(content_id, channels=channels)
            hit.update({"member_ids": actual_members, "locator": actual_locator})
            return hit

    report = evaluate_gold_set(
        WrongChunkReader("gen-candidate", ("archive",)),
        gold,
        allow_small=True,
    )

    assert report["metrics"]["recall_at_50"] == 1.0
    assert report["results"][0]["citation_ok"] is False
    assert report["metrics"]["citation_accuracy"] == 0.0
    assert "citation_accuracy_below_1.00" in report["failure_reasons"]


def test_absolute_timeout_checks_each_query_even_when_p95_is_below_8s(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(
        knowledge_eval,
        "verify_generation",
        lambda *_args, **_kwargs: {
            "ok": True,
            "stats": {"coverage": 1.0, "orphans": 0},
            "errors": [],
        },
    )
    durations = iter([0.001] * 19 + [8.001])
    clock = 0.0
    pending_duration = 0.0

    def perf_counter() -> float:
        nonlocal clock, pending_duration
        if pending_duration == 0.0:
            pending_duration = next(durations)
            return clock
        clock += pending_duration
        pending_duration = 0.0
        return clock

    monkeypatch.setattr(knowledge_eval.time, "perf_counter", perf_counter)
    gold = _write_gold(tmp_path / "latency-gold.jsonl", count=20)
    report = evaluate_gold_set(
        _FakeReader("gen-candidate", ("archive", "podcast")),
        gold,
        allow_small=True,
    )

    assert report["metrics"]["e2e_p95_ms"] == pytest.approx(1.0)
    assert report["metrics"]["max_e2e_ms"] == pytest.approx(8001.0)
    assert report["metrics"]["absolute_timeout_violation_count"] == 1
    assert "e2e_p95_over_8s" not in report["failure_reasons"]
    assert "absolute_timeout_violation" in report["failure_reasons"]


def test_release_pair_requires_gold_for_every_active_candidate_source(tmp_path, monkeypatch) -> None:
    _gold, report, _candidate, _baseline = _release_report(
        tmp_path,
        monkeypatch,
        candidate_sources=("archive", "podcast", "telegram_archive"),
    )

    assert report["passed"] is False
    assert "gold_set_missing_required_source_kinds" in report["failure_reasons"]
    assert report["required_source_kinds"] == ["archive", "podcast", "telegram_archive"]


def test_activation_rejects_one_row_gold_with_fabricated_query_count(tmp_path, monkeypatch) -> None:
    _gold, report, candidate, baseline = _release_report(tmp_path, monkeypatch)
    one_row = tmp_path / "one-row.jsonl"
    one_row.write_text(json.dumps(_gold_case(0)) + "\n", encoding="utf-8")
    rows = knowledge_eval._read_gold(one_row)
    report["gold_set"] = str(one_row)
    report["gold_set_hash"] = sha256_text(one_row.read_text(encoding="utf-8"))
    report["query_set_hash"] = knowledge_eval._query_set_hash(rows)
    _reseal(report)
    evaluation = _write_report(tmp_path / "fabricated.json", report)

    with pytest.raises(ValueError, match="gold structure"):
        validate_evaluation_for_activation(
            evaluation, "gen-candidate", **_live_generation_kwargs(candidate, baseline)
        )


def test_same_run_release_pair_passes_activation_and_rechecks_gold(tmp_path, monkeypatch) -> None:
    gold, report, candidate, baseline = _release_report(tmp_path, monkeypatch)
    evaluation = _write_report(tmp_path / "evaluation.json", report)

    assert report["passed"] is True
    assert report["metrics"]["query_count"] == 200
    assert report["gold_structure"]["query_count"] == 200
    assert report["baseline_generation_id"] == "gen-baseline"
    assert report["baseline_provenance"]["same_run"] is True
    assert report["baseline_provenance"]["candidate_query_set_hash"] == report["query_set_hash"]
    assert (
        report["baseline_provenance"]["candidate_artifact_identity"]
        == report["artifact_identity"]
    )
    assert (
        report["baseline_provenance"]["baseline_artifact_identity"]
        == report["paired_baseline_evaluation"]["artifact_identity"]
    )
    assert report["results"][0]["kind"] == "exact"
    assert report["results"][0]["source_kind"] == "archive"
    assert validate_evaluation_for_activation(
        evaluation, "gen-candidate", **_live_generation_kwargs(candidate, baseline)
    )["passed"] is True

    rows = gold.read_text(encoding="utf-8").splitlines()
    changed = json.loads(rows[0])
    changed["query"] = "changed query"
    rows[0] = json.dumps(changed)
    gold.write_text("\n".join(rows) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="gold set hash changed"):
        validate_evaluation_for_activation(
            evaluation, "gen-candidate", **_live_generation_kwargs(candidate, baseline)
        )


@pytest.mark.parametrize("rebuilt_role", ["candidate", "baseline"])
def test_activation_rejects_same_generation_id_rebuilt_artifact(
    tmp_path, monkeypatch, rebuilt_role: str
) -> None:
    _gold, report, candidate, baseline = _release_report(tmp_path, monkeypatch)
    evaluation = _write_report(tmp_path / f"rebuilt-{rebuilt_role}.json", report)
    original = candidate if rebuilt_role == "candidate" else baseline
    moved = tmp_path / f"moved-{rebuilt_role}"
    original.generation_dir.rename(moved)
    rebuilt = _FakeReader(
        str(original.manifest["generation_id"]),
        ("archive", "podcast"),
        generation_root=tmp_path / "generations",
        artifact_salt="rebuilt",
    )
    live_candidate = rebuilt if rebuilt_role == "candidate" else candidate
    live_baseline = rebuilt if rebuilt_role == "baseline" else baseline

    with pytest.raises(ValueError, match=f"evaluation {rebuilt_role} artifact"):
        validate_evaluation_for_activation(
            evaluation,
            "gen-candidate",
            **_live_generation_kwargs(live_candidate, live_baseline),
        )


def test_activation_retry_allows_ready_to_active_manifest_only_mutation(
    tmp_path, monkeypatch
) -> None:
    _gold, report, candidate, baseline = _release_report(tmp_path, monkeypatch)
    evaluation = _write_report(tmp_path / "activation-retry.json", report)
    manifest_path = candidate.generation_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.update({"status": "active", "activated_at": "2026-08-26T00:00:00+00:00"})
    manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")

    live_identity = knowledge_eval.generation_artifact_identity(candidate.generation_dir)
    assert live_identity["manifest_sha256"] != report["artifact_identity"]["manifest_sha256"]
    assert validate_evaluation_for_activation(
        evaluation,
        "gen-candidate",
        **_live_generation_kwargs(candidate, baseline),
    )["passed"] is True


@pytest.mark.parametrize("field", ["manifest_hash", "catalog_hash", "vectors_hash"])
def test_activation_retry_rejects_stable_artifact_identity_mutation(
    tmp_path, monkeypatch, field: str
) -> None:
    _gold, report, candidate, baseline = _release_report(tmp_path, monkeypatch)
    evaluation = _write_report(tmp_path / f"stable-mutation-{field}.json", report)
    manifest_path = candidate.generation_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if field == "catalog_hash":
        catalog = candidate.generation_dir / "catalog.sqlite"
        catalog.write_bytes(b"rebuilt catalog")
        manifest[field] = knowledge_eval.sha256_file(catalog)
    elif field == "vectors_hash":
        vectors = candidate.generation_dir / "vectors.f32"
        vectors.write_bytes(b"rebuilt vectors")
        manifest[field] = knowledge_eval.sha256_file(vectors)
    else:
        manifest[field] = "f" * 64
    manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")

    with pytest.raises(ValueError, match="evaluation candidate artifact"):
        validate_evaluation_for_activation(
            evaluation,
            "gen-candidate",
            **_live_generation_kwargs(candidate, baseline),
        )


def test_generation_artifact_identity_rejects_duplicate_manifest_key(tmp_path) -> None:
    reader = _FakeReader(
        "gen-a", ("archive",), generation_root=tmp_path / "generations"
    )
    manifest_path = reader.generation_dir / "manifest.json"
    manifest_path.write_text(
        manifest_path.read_text(encoding="utf-8")[:-1] + ',"generation_id":"gen-a"}',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="duplicate JSON key"):
        knowledge_eval.generation_artifact_identity(reader.generation_dir)


def test_generation_artifact_identity_rejects_invalid_hash_and_manifest_symlink(
    tmp_path,
) -> None:
    reader = _FakeReader(
        "gen-a", ("archive",), generation_root=tmp_path / "generations"
    )
    manifest_path = reader.generation_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["catalog_hash"] = "not-sha256"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="invalid catalog_hash"):
        knowledge_eval.generation_artifact_identity(reader.generation_dir)

    target = tmp_path / "manifest-target.json"
    target.write_text(json.dumps(reader.manifest), encoding="utf-8")
    manifest_path.unlink()
    manifest_path.symlink_to(target)
    with pytest.raises(ValueError, match="regular non-symlink"):
        knowledge_eval.generation_artifact_identity(reader.generation_dir)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("max_e2e_ms", 7999.0, "max E2E metric"),
        ("absolute_timeout_violation_count", 1, "timeout violation count"),
    ],
)
def test_activation_recomputes_absolute_timeout_evidence(
    tmp_path, monkeypatch, field: str, value: Any, message: str
) -> None:
    _gold, report, candidate, baseline = _release_report(tmp_path, monkeypatch)
    report["metrics"][field] = value
    _reseal(report)
    evaluation = _write_report(tmp_path / f"tampered-{field}.json", report)

    with pytest.raises(ValueError, match=message):
        validate_evaluation_for_activation(
            evaluation, "gen-candidate", **_live_generation_kwargs(candidate, baseline)
        )


def test_bare_baseline_float_cannot_satisfy_formal_release(tmp_path, monkeypatch) -> None:
    _prepare_evaluation(monkeypatch)
    gold = _write_gold(tmp_path / "gold.jsonl")
    candidate = _FakeReader("gen-candidate", ("archive", "podcast"))

    report = evaluate_gold_set(candidate, gold, baseline_e2e_p95_ms=1.0)

    assert report["passed"] is False
    assert report["metrics"]["baseline_e2e_p95_ms"] is None
    assert report["diagnostic_baseline_e2e_p95_ms"] == 1.0
    assert "baseline_provenance_missing" in report["failure_reasons"]


def test_exact_recall_counts_every_relevant_identity(tmp_path, monkeypatch) -> None:
    _prepare_evaluation(monkeypatch)
    case = _gold_case(0)
    gold = tmp_path / "exact.jsonl"
    gold.write_text(json.dumps(case) + "\n", encoding="utf-8")

    class PartialExactReader(_FakeReader):
        def search(self, query: str, *, top_k: int, use_reranker: bool) -> dict[str, Any]:
            del query, use_reranker
            ids = [case["relevant_content_ids"][0], "filler-a", "filler-b"]
            return {
                "hits": [self._hit(content_id) for content_id in ids[:top_k]],
                "timing_ms": {"lookup": 10.0, "total": 12.0},
                "degraded_reasons": [],
            }

    reader = PartialExactReader("gen-candidate", ("archive",))
    report = evaluate_gold_set(reader, gold, allow_small=True)

    assert report["metrics"]["exact_recall"] == 0.25


def test_exact_recall_does_not_credit_dense_only_relevant_hits(tmp_path, monkeypatch) -> None:
    _prepare_evaluation(monkeypatch)
    case = _gold_case(0)
    gold = tmp_path / "exact-dense-only.jsonl"
    gold.write_text(json.dumps(case) + "\n", encoding="utf-8")

    class DenseOnlyExactReader(_FakeReader):
        def search(self, query: str, *, top_k: int, use_reranker: bool) -> dict[str, Any]:
            del query, use_reranker
            hits = [
                self._hit(content_id, channels=("dense",))
                for content_id in case["relevant_content_ids"]
            ]
            return {
                "hits": hits[:top_k],
                "timing_ms": {"lookup": 10.0, "total": 12.0},
                "degraded_reasons": [],
            }

    report = evaluate_gold_set(
        DenseOnlyExactReader("gen-candidate", ("archive",)),
        gold,
        allow_small=True,
    )

    assert report["metrics"]["recall_at_50"] == 1.0
    assert report["metrics"]["exact_recall"] == 0.0
    assert report["results"][0]["exact_matched"] == []
    assert "exact_recall_below_1.00" in report["failure_reasons"]


def test_paired_evaluation_embeds_same_run_extractable_baseline(tmp_path, monkeypatch) -> None:
    _gold, report, _candidate, _baseline = _release_report(tmp_path, monkeypatch)
    paired = _write_report(tmp_path / "paired.json", report)

    baseline = knowledge_eval.extract_paired_baseline_evaluation(paired)

    assert baseline["generation_id"] == "gen-baseline"
    assert baseline["paired_candidate_generation_id"] == "gen-candidate"
    assert baseline["measurement_run_id"] == report["measurement_run_id"]
    assert baseline["gold_set_hash"] == report["gold_set_hash"]
    assert baseline["query_set_hash"] == report["query_set_hash"]
    assert baseline["report_hash"] == knowledge_eval._report_hash(baseline)

    extracted = tmp_path / "baseline.json"
    assert (
        knowledge_eval._module_main(
            ["extract-paired-baseline", str(paired), str(extracted)]
        )
        == 0
    )
    assert json.loads(extracted.read_text(encoding="utf-8")) == baseline


def test_paired_baseline_extraction_rejects_resealed_cross_run_substitution(
    tmp_path, monkeypatch
) -> None:
    _gold, report, _candidate, _baseline = _release_report(tmp_path, monkeypatch)
    report["paired_baseline_evaluation"]["measurement_run_id"] = "0" * 32
    report["paired_baseline_evaluation"] = knowledge_eval._seal_report(
        report["paired_baseline_evaluation"]
    )
    _reseal(report)
    paired = _write_report(tmp_path / "substituted.json", report)

    with pytest.raises(ValueError, match="run binding"):
        knowledge_eval.extract_paired_baseline_evaluation(paired)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("baseline_generation_id", "gen-candidate"),
        ("baseline_query_set_hash", "b" * 64),
        ("baseline_query_count", 199),
        ("candidate_e2e_p95_ms", 2.0),
        ("host_fingerprint", "b" * 64),
    ],
)
def test_activation_rejects_resealed_baseline_provenance_tampering(
    tmp_path, monkeypatch, field: str, value: Any
) -> None:
    _gold, original, candidate, baseline = _release_report(tmp_path, monkeypatch)
    report = copy.deepcopy(original)
    report["baseline_provenance"][field] = value
    _reseal(report)
    evaluation = _write_report(tmp_path / f"tampered-{field}.json", report)

    with pytest.raises(ValueError, match="evaluation baseline"):
        validate_evaluation_for_activation(
            evaluation, "gen-candidate", **_live_generation_kwargs(candidate, baseline)
        )


def test_activation_rejects_unsealed_report_tampering(tmp_path, monkeypatch) -> None:
    _gold, report, candidate, baseline = _release_report(tmp_path, monkeypatch)
    report["baseline_provenance"]["measurement_run_id"] = "tampered"
    evaluation = _write_report(tmp_path / "tampered-report.json", report)

    with pytest.raises(ValueError, match="report hash mismatch"):
        validate_evaluation_for_activation(
            evaluation, "gen-candidate", **_live_generation_kwargs(candidate, baseline)
        )


def test_activation_validation_rejects_unknown_schema(tmp_path) -> None:
    evaluation = tmp_path / "evaluation.json"
    evaluation.write_text('{"schema":"other"}\n', encoding="utf-8")

    with pytest.raises(ValueError, match="unknown knowledge evaluation schema"):
        validate_evaluation_for_activation(
            evaluation,
            "gen-a",
            candidate_generation_dir=tmp_path / "gen-a",
            baseline_generation_dir=tmp_path / "gen-b",
        )


def test_quality_gates_fail_closed_when_required_gold_strata_are_missing() -> None:
    metrics = _passing_metrics()
    metrics["kind_counts"] = {"exact": 39, "semantic": 60, "cross_source": 40, "long_form": 30}
    metrics["exact_recall"] = None
    metrics["citation_accuracy"] = None
    metrics["baseline_e2e_p95_ms"] = None

    reasons = quality_gate_reasons(metrics)

    assert "baseline_e2e_p95_ms_missing" in reasons
    assert "citation_accuracy_missing" in reasons
    assert "exact_recall_missing" in reasons
    assert "gold_set_exact_below_40" in reasons


def test_guard_evaluation_regression_is_same_gold_and_per_stratum() -> None:
    baseline = {
        "schema": "chatdaily-knowledge-evaluation.v1",
        "generation_id": "gen-a",
        "gold_set_hash": "a" * 64,
        "metrics": _passing_metrics(),
    }
    current_metrics = _passing_metrics()
    current_metrics["recall_at_50"] = 0.91
    current_metrics["stratum_ndcg_at_5"] = {"archive": 0.70, "podcast": 0.84}
    current = {
        "schema": "chatdaily-knowledge-evaluation.v1",
        "generation_id": "gen-b",
        "gold_set_hash": "a" * 64,
        "metrics": current_metrics,
    }

    result = evaluation_regression_metrics(current, baseline)

    assert result["recall_at_50"]["drop"] == pytest.approx(0.05)
    assert result["stratum_ndcg_at_5"]["max_drop"] == pytest.approx(0.10)
    assert result["stratum_ndcg_at_5"]["values"]["archive"]["drop"] == pytest.approx(0.10)

    current["gold_set_hash"] = "b" * 64
    with pytest.raises(ValueError, match="same frozen gold set"):
        evaluation_regression_metrics(current, baseline)
