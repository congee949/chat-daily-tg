from __future__ import annotations

import argparse
import copy
import json
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import numpy as np
import pytest

from chat_daily_tg import knowledge_cli


_REAL_GENERATION_SHADOW_CONTEXT = knowledge_cli._generation_shadow_context


def _stub_context(generation_id: str) -> dict[str, object]:
    baseline = generation_id in {"gen-a", "gen-v0", "gen-v1"} or "baseline" in generation_id
    return {
        "schema": knowledge_cli.GENERATION_CONTEXT_SCHEMA,
        "generation_id": generation_id,
        "artifact_sha256": ("d" if baseline else "c") * 64,
        "manifest_hash": "e" * 64,
        "catalog_hash": "f" * 64,
        "vectors_hash": "0" * 64,
        "model_id": "embed-test",
        "model_revision": "embed-revision",
        "reranker_model_id": "rerank-test",
        "reranker_revision": "rerank-revision",
        "dimension": 4096,
    }


def _safe_test_process_method() -> str:
    available = knowledge_cli.multiprocessing.get_all_start_methods()
    return "forkserver" if "forkserver" in available else "spawn"


def _deadline_test_worker(
    args: argparse.Namespace,
    generation: str,
    *,
    lexical_only: bool,
    connection: object,
) -> None:
    """Picklable worker used to test process control without unsafe test forks."""

    mode = args._test_worker_mode
    lexical = {
        "generation_id": generation,
        "dense_enabled": False,
        "degraded": True,
        "degraded_reasons": ["lexical_only"],
        "reranker_used": False,
        "timing_ms": {"total": 1.0},
        "hits": [{"content_id": "trusted-lexical-hit"}],
    }
    try:
        if mode == "invalid":
            connection.send(
                {
                    "ok": False,
                    "phase": "final",
                    "error_type": "ValueError",
                    "error": "generation catalog seal mismatch; lexical fallback is unsafe",
                }
            )
            return
        if mode == "lexical":
            if not lexical_only:
                raise AssertionError("preflight should select lexical-only worker")
            connection.send({"ok": True, "phase": "final", "result": lexical})
            return
        connection.send({"ok": True, "phase": "snapshot", "result": lexical})
        if mode == "slow":
            time.sleep(2.0)
            return
        if mode == "fail_after_snapshot":
            connection.send(
                {
                    "ok": False,
                    "phase": "final",
                    "error_type": "RuntimeError",
                    "error": "dense worker crashed",
                }
            )
            return
        connection.send(
            {
                "ok": True,
                "phase": "final",
                "result": {
                    **lexical,
                    "dense_enabled": True,
                    "degraded": False,
                    "degraded_reasons": [],
                    "reranker_used": True,
                    "hits": [{"content_id": "full-hit"}],
                },
            }
        )
    finally:
        connection.close()


@pytest.fixture(autouse=True)
def _isolate_generation_context(monkeypatch: pytest.MonkeyPatch) -> None:
    """Most CLI unit tests use mocked readers/pointers rather than artifacts."""

    monkeypatch.setattr(
        knowledge_cli,
        "_generation_shadow_context",
        lambda _root, generation_id: _stub_context(generation_id),
    )


def _pending_release(
    candidate: str = "gen-b", baseline: str = "gen-a"
) -> dict[str, object]:
    return {
        "schema": "chatdaily-knowledge-release.v1",
        "status": "canary_pending",
        "candidate_generation": candidate,
        "baseline_generation": baseline,
        "candidate_artifact_sha256": "c" * 64,
        "baseline_artifact_sha256": "d" * 64,
        "evaluation_hash": "a" * 64,
        "shadow_journal_hash": "b" * 64,
        "prepared_at": "2026-08-25T10:00:00+00:00",
    }


def _write_semantic_config(
    path: Path, *, regular: bool = False, canary: bool = False
) -> None:
    path.write_text(
        f"""
sources: {{wechat: {{groups: [test]}}}}
semantic_features:
  knowledge_canary_enabled: {str(canary).lower()}
  knowledge_retrieval_enabled: {str(regular).lower()}
models:
  summary: {{endpoint: "http://summary", model: "summary", api_key_env: "K"}}
telegram: {{bot_token_env: "TT", chat_id_env: "TC"}}
""",
        encoding="utf-8",
    )


def _ready_shadow(*, canary: bool = True) -> dict[str, object]:
    return {
        "ready": True,
        "failure_reasons": [],
        "canary_ready": canary,
        "canary_failure_reasons": [] if canary else ["canary_samples_insufficient"],
    }


def _release_args(tmp_path: Path) -> argparse.Namespace:
    evaluation = tmp_path / "evaluation.json"
    evaluation.write_text('{"passed":true}\n', encoding="utf-8")
    shadow = tmp_path / "shadow.jsonl"
    shadow.write_text('{"kind":"fixture"}\n', encoding="utf-8")
    index_root = tmp_path / "index"
    index_root.mkdir()
    return argparse.Namespace(
        evaluation=evaluation,
        shadow_journal=shadow,
        generation="gen-b",
        index_root=index_root,
    )


class _Reader:
    def __init__(self, generation: str, *, fail: bool = False):
        self.generation = generation
        self.fail = fail
        self.closed = False

    def search(
        self,
        query: str,
        *,
        top_k: int,
        use_reranker: bool,
        use_dense: bool = True,
        timeout: float = 8.0,
    ):
        del use_dense, timeout
        if self.fail:
            raise ValueError("candidate invalid")
        return {
            "generation_id": self.generation,
            "degraded_reasons": [],
            "hits": [{"content_id": "content:1"}],
        }

    def close(self) -> None:
        self.closed = True


def _direct_query(reader_factory, args, generation):
    instance = reader_factory(argparse.Namespace(generation=generation))
    try:
        return instance.search(
            args.query,
            top_k=args.top_k,
            use_reranker=not args.no_rerank,
        )
    finally:
        instance.close()


def test_online_commands_default_to_eight_second_client_timeout() -> None:
    parser = knowledge_cli.build_parser()

    query = parser.parse_args(["query", "hello"])
    canary = parser.parse_args(
        ["canary-query", "hello", "--request-id", "r1", "--candidate", "gen-b"]
    )
    diagnostic = parser.parse_args(
        ["diagnostic-query", "hello", "--generation", "gen-old"]
    )
    build = parser.parse_args(["build"])

    assert query.timeout == 8.0
    assert canary.timeout == 8.0
    assert diagnostic.timeout == 8.0
    assert build.timeout == 180.0
    assert query.enable_retrieval is False
    assert canary.enable_retrieval is False
    assert not hasattr(query, "no_record")
    with pytest.raises(SystemExit):
        parser.parse_args(["query", "hello", "--no-record"])


def test_query_process_start_method_uses_fork_only_for_single_native_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        knowledge_cli.multiprocessing,
        "get_all_start_methods",
        lambda: ["spawn", "fork", "forkserver"],
    )
    monkeypatch.setattr(knowledge_cli, "_native_thread_count", lambda: 1)
    assert knowledge_cli._query_process_start_method() == "fork"

    monkeypatch.setattr(knowledge_cli, "_native_thread_count", lambda: 2)
    assert knowledge_cli._query_process_start_method() == "forkserver"
    assert knowledge_cli._query_process_start_method("fork") is None

    monkeypatch.setattr(knowledge_cli, "_native_thread_count", lambda: None)
    assert knowledge_cli._query_process_start_method() == "forkserver"


def test_query_process_start_method_falls_back_when_fork_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        knowledge_cli.multiprocessing,
        "get_all_start_methods",
        lambda: ["spawn"],
    )
    monkeypatch.setattr(knowledge_cli, "_native_thread_count", lambda: 1)

    assert knowledge_cli._query_process_start_method() == "spawn"
    assert knowledge_cli._query_process_start_method("fork") is None
    assert knowledge_cli._query_process_start_method("spawn") == "spawn"


def test_native_thread_count_parses_macos_ps_and_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(knowledge_cli.sys, "platform", "darwin")
    monkeypatch.setattr(
        knowledge_cli.subprocess,
        "run",
        lambda *_args, **_kwargs: argparse.Namespace(
            returncode=0,
            stdout="USER PID COMMAND\nApple 123 python\nApple 123 python\n",
        ),
    )
    assert knowledge_cli._native_thread_count() == 2

    monkeypatch.setattr(
        knowledge_cli.subprocess,
        "run",
        lambda *_args, **_kwargs: argparse.Namespace(returncode=1, stdout=""),
    )
    assert knowledge_cli._native_thread_count() is None


def _reader_test_args(tmp_path: Path) -> argparse.Namespace:
    return argparse.Namespace(
        index_root=tmp_path,
        generation="gen-a",
        runtime_config=tmp_path / "runtime.json",
        endpoint=None,
        batch_size=32,
        timeout=8.0,
    )


def _reader_test_manifest() -> dict[str, object]:
    manifest = {
        "schema_version": knowledge_cli.SCHEMA_VERSION,
        "generation_id": "gen-a",
        "status": "ready",
        "row_count": 1,
        "catalog_hash": "a" * 64,
        "vectors_hash": "b" * 64,
        **knowledge_cli.GenerationConfig(
            model_id="embed-test",
            model_revision="embed-revision",
            reranker_model_id="rerank-test",
            reranker_revision="rerank-revision",
            dimension=3,
        ).identity(),
    }
    manifest["manifest_hash"] = knowledge_cli.compute_manifest_hash(manifest)
    return manifest


def test_reader_overlaps_tokenizer_with_seal_and_joins_before_reader(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    counter_started = threading.Event()
    seal_started = threading.Event()
    counter_ready = threading.Event()
    state = (_reader_test_manifest(), {"ok": True}, None)

    def counter(_path: Path) -> object:
        counter_started.set()
        assert seal_started.wait(1.0)
        counter_ready.set()
        return object()

    def online(_path: Path, *, expected: object) -> object:
        assert expected is not None
        seal_started.set()
        assert counter_started.wait(1.0)
        return state

    sentinel = object()

    def reader(*_args: object, _online_state: object, **_kwargs: object) -> object:
        assert counter_ready.is_set()
        assert not any(
            thread.name.startswith("knowledge-tokenizer")
            for thread in threading.enumerate()
        )
        assert _online_state is state
        return sentinel

    monkeypatch.setattr(knowledge_cli, "_generation_dir", lambda _args: tmp_path / "gen-a")
    monkeypatch.setattr(knowledge_cli, "load_manifest", lambda _path: _reader_test_manifest())
    monkeypatch.setattr(
        knowledge_cli,
        "_runtime_config",
        lambda _path: {"embedding_path": str(tmp_path / "model")},
    )
    monkeypatch.setattr(knowledge_cli, "TokenCounter", counter)
    monkeypatch.setattr(knowledge_cli, "_open_online_verified_generation", online)
    monkeypatch.setattr(knowledge_cli, "QwenRuntimeClient", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(knowledge_cli, "GenerationReader", reader)

    assert knowledge_cli._reader(_reader_test_args(tmp_path)) is sentinel


def test_reader_closes_verified_connection_when_tokenizer_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class Connection:
        closed = False

        def close(self) -> None:
            self.closed = True

    connection = Connection()
    state = (_reader_test_manifest(), {"ok": True}, connection)
    monkeypatch.setattr(knowledge_cli, "_generation_dir", lambda _args: tmp_path / "gen-a")
    monkeypatch.setattr(knowledge_cli, "load_manifest", lambda _path: _reader_test_manifest())
    monkeypatch.setattr(
        knowledge_cli,
        "_runtime_config",
        lambda _path: {"embedding_path": str(tmp_path / "model")},
    )
    monkeypatch.setattr(
        knowledge_cli,
        "TokenCounter",
        lambda _path: (_ for _ in ()).throw(RuntimeError("tokenizer failed")),
    )
    monkeypatch.setattr(
        knowledge_cli, "_open_online_verified_generation", lambda *_args, **_kwargs: state
    )

    with pytest.raises(RuntimeError, match="tokenizer failed"):
        knowledge_cli._reader(_reader_test_args(tmp_path))

    assert connection.closed is True


def test_query_process_sends_snapshot_before_preembedding_finishes_and_joins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release_embedding = threading.Event()
    snapshot_sent = threading.Event()
    pool_joined = threading.Event()
    prepared = object()

    class BlockingFuture:
        def result(self) -> object:
            assert release_embedding.wait(2.0)
            return prepared

    class Pool:
        def shutdown(self, *, wait: bool, cancel_futures: bool) -> None:
            assert wait is True and cancel_futures is True
            assert release_embedding.is_set()
            pool_joined.set()

    class Reader:
        closed = False

        def search(
            self,
            _query: str,
            *,
            use_dense: bool,
            precomputed_query: object = None,
            **_kwargs: object,
        ) -> dict[str, object]:
            if use_dense:
                assert pool_joined.is_set()
                assert precomputed_query is prepared
            return {
                "generation_id": "gen-a",
                "dense_enabled": use_dense,
                "degraded": False,
                "degraded_reasons": [],
                "reranker_used": use_dense,
                "timing_ms": {"total": 1.0},
                "hits": [{"content_id": "full" if use_dense else "lexical"}],
            }

        def close(self) -> None:
            self.closed = True

    class Connection:
        def __init__(self) -> None:
            self.messages: list[dict[str, object]] = []
            self.closed = False

        def send(self, value: dict[str, object]) -> None:
            self.messages.append(value)
            if value.get("phase") == "snapshot":
                snapshot_sent.set()

        def close(self) -> None:
            self.closed = True

    reader = Reader()
    pool = Pool()
    connection = Connection()
    monkeypatch.setattr(
        knowledge_cli,
        "_query_reader",
        lambda _args: (reader, pool, BlockingFuture()),
    )
    args = argparse.Namespace(
        query="hello",
        top_k=8,
        no_rerank=False,
        timeout=2.0,
    )
    worker = threading.Thread(
        target=knowledge_cli._query_process,
        args=(args, "gen-a"),
        kwargs={"lexical_only": False, "connection": connection},
    )
    worker.start()

    assert snapshot_sent.wait(1.0)
    assert worker.is_alive()
    assert [message["phase"] for message in connection.messages] == ["snapshot"]
    release_embedding.set()
    worker.join(2.0)

    assert not worker.is_alive()
    assert [message["phase"] for message in connection.messages] == [
        "snapshot",
        "final",
    ]
    assert pool_joined.is_set()
    assert reader.closed is True
    assert connection.closed is True


def test_real_query_reader_overlaps_seal_and_embed_then_snapshots_before_join(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    counter_started = threading.Event()
    seal_started = threading.Event()
    embed_started = threading.Event()
    release_embedding = threading.Event()
    snapshot_sent = threading.Event()

    class Counter:
        def __init__(self, _path: Path) -> None:
            counter_started.set()
            assert seal_started.wait(1.0)

        def count(self, _text: str) -> int:
            return 1

    class Client:
        def embed_queries(self, _texts: list[str], *, timeout: float) -> list[object]:
            assert timeout > 0
            embed_started.set()
            assert release_embedding.wait(2.0)
            return [object()]

    class CatalogConnection:
        closed = False

        def close(self) -> None:
            self.closed = True

    catalog_connection = CatalogConnection()
    state = (_reader_test_manifest(), {"ok": True}, catalog_connection)

    class Reader:
        def __init__(
            self,
            _generation_dir: Path,
            _client: object,
            _counter: object,
            *,
            expected: object,
            _online_state: object,
        ) -> None:
            assert expected is not None
            assert _online_state is state

        def search(
            self,
            _query: str,
            *,
            use_dense: bool,
            precomputed_query: object = None,
            **_kwargs: object,
        ) -> dict[str, object]:
            if use_dense:
                assert release_embedding.is_set()
                assert precomputed_query is not None
                assert not any(
                    thread.name.startswith("knowledge-query-init")
                    for thread in threading.enumerate()
                )
            return {
                "generation_id": "gen-a",
                "dense_enabled": use_dense,
                "degraded": False,
                "degraded_reasons": [],
                "reranker_used": use_dense,
                "timing_ms": {"total": 1.0},
                "hits": [{"content_id": "full" if use_dense else "lexical"}],
            }

        def close(self) -> None:
            catalog_connection.close()

    class Connection:
        def __init__(self) -> None:
            self.messages: list[dict[str, object]] = []
            self.closed = False

        def send(self, value: dict[str, object]) -> None:
            self.messages.append(value)
            if value.get("phase") == "snapshot":
                snapshot_sent.set()

        def close(self) -> None:
            self.closed = True

    def verify_online(*_args: object, **_kwargs: object) -> object:
        seal_started.set()
        assert counter_started.wait(1.0)
        assert embed_started.wait(1.0)
        return state

    monkeypatch.setattr(knowledge_cli, "_generation_dir", lambda _args: tmp_path / "gen-a")
    monkeypatch.setattr(knowledge_cli, "load_manifest", lambda _path: _reader_test_manifest())
    monkeypatch.setattr(
        knowledge_cli,
        "_runtime_config",
        lambda _path: {"embedding_path": str(tmp_path / "model")},
    )
    monkeypatch.setattr(knowledge_cli, "TokenCounter", Counter)
    monkeypatch.setattr(
        knowledge_cli, "_reader_runtime_client", lambda *_args, **_kwargs: Client()
    )
    monkeypatch.setattr(
        knowledge_cli, "_open_online_verified_generation", verify_online
    )
    monkeypatch.setattr(knowledge_cli, "GenerationReader", Reader)
    connection = Connection()
    args = _reader_test_args(tmp_path)
    args.query = "hello"
    args.top_k = 8
    args.no_rerank = False
    worker = threading.Thread(
        target=knowledge_cli._query_process,
        args=(args, "gen-a"),
        kwargs={"lexical_only": False, "connection": connection},
    )
    worker.start()

    assert snapshot_sent.wait(1.0)
    assert embed_started.is_set()
    assert worker.is_alive()
    assert [message["phase"] for message in connection.messages] == ["snapshot"]
    release_embedding.set()
    worker.join(2.0)

    assert not worker.is_alive()
    assert [message["phase"] for message in connection.messages] == [
        "snapshot",
        "final",
    ]
    assert catalog_connection.closed is True
    assert connection.closed is True
    assert not any(
        thread.name.startswith("knowledge-query-init")
        for thread in threading.enumerate()
    )


def test_query_reader_discards_successful_preembedding_when_seal_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    embedded = threading.Event()
    embed_calls = 0

    class Counter:
        def __init__(self, _path: Path) -> None:
            return None

        def count(self, _text: str) -> int:
            return 1

    class Client:
        def embed_queries(self, _texts: list[str], *, timeout: float) -> list[object]:
            nonlocal embed_calls
            assert timeout > 0
            embed_calls += 1
            embedded.set()
            return [object()]

    monkeypatch.setattr(knowledge_cli, "_generation_dir", lambda _args: tmp_path / "gen-a")
    monkeypatch.setattr(knowledge_cli, "load_manifest", lambda _path: _reader_test_manifest())
    monkeypatch.setattr(
        knowledge_cli,
        "_runtime_config",
        lambda _path: {"embedding_path": str(tmp_path / "model")},
    )
    monkeypatch.setattr(knowledge_cli, "TokenCounter", Counter)
    monkeypatch.setattr(
        knowledge_cli, "_reader_runtime_client", lambda *_args, **_kwargs: Client()
    )

    def reject_seal(*_args: object, **_kwargs: object) -> object:
        assert embedded.wait(1.0)
        raise ValueError("catalog seal mismatch")

    monkeypatch.setattr(
        knowledge_cli, "_open_online_verified_generation", reject_seal
    )
    monkeypatch.setattr(
        knowledge_cli,
        "GenerationReader",
        lambda *_args, **_kwargs: pytest.fail("unsealed reader must not be built"),
    )

    args = _reader_test_args(tmp_path)
    args.query = "hello"
    with pytest.raises(ValueError, match="catalog seal mismatch"):
        knowledge_cli._query_reader(args)

    assert embed_calls == 1
    assert not any(
        thread.name.startswith("knowledge-query-init")
        for thread in threading.enumerate()
    )


def test_query_reader_rejects_invalid_manifest_before_outbound_embedding(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    manifest = _reader_test_manifest()
    manifest["manifest_hash"] = "0" * 64
    monkeypatch.setattr(knowledge_cli, "_generation_dir", lambda _args: tmp_path / "gen-a")
    monkeypatch.setattr(knowledge_cli, "load_manifest", lambda _path: manifest)
    monkeypatch.setattr(
        knowledge_cli,
        "_runtime_config",
        lambda _path: pytest.fail("invalid manifest must fail before runtime setup"),
    )
    args = _reader_test_args(tmp_path)
    args.query = "hello"

    with pytest.raises(ValueError, match="manifest_hash_mismatch"):
        knowledge_cli._query_reader(args)


def test_hard_deadline_kills_full_worker_and_returns_lexical_fallback(
) -> None:
    args = argparse.Namespace(
        query="hello",
        top_k=8,
        no_rerank=False,
        timeout=1.0,
        _test_worker_mode="slow",
    )
    started = time.monotonic()

    result = knowledge_cli._query_generation_hard_deadline(
        args,
        "gen-a",
        deadline=time.monotonic() + 1.0,
        start_method=_safe_test_process_method(),
        _worker_target=_deadline_test_worker,
    )

    assert time.monotonic() - started < 1.25
    assert result["hits"][0]["content_id"] == "trusted-lexical-hit"
    assert "online_hard_deadline_exceeded" in result["degraded_reasons"]


def test_full_worker_publishes_trusted_snapshot_before_dense_result(
) -> None:
    args = argparse.Namespace(
        query="hello", top_k=8, no_rerank=False, timeout=2.0, _test_worker_mode="ordered"
    )

    result = knowledge_cli._query_generation_hard_deadline(
        args,
        "gen-a",
        deadline=time.monotonic() + 2.0,
        start_method=_safe_test_process_method(),
        _worker_target=_deadline_test_worker,
    )

    assert result["hits"][0]["content_id"] == "full-hit"
    assert result["reranker_used"] is True


def test_full_worker_failure_after_snapshot_returns_last_trusted_snapshot(
) -> None:
    args = argparse.Namespace(
        query="hello",
        top_k=8,
        no_rerank=False,
        timeout=2.0,
        _test_worker_mode="fail_after_snapshot",
    )

    result = knowledge_cli._query_generation_hard_deadline(
        args,
        "gen-a",
        deadline=time.monotonic() + 2.0,
        start_method=_safe_test_process_method(),
        _worker_target=_deadline_test_worker,
    )

    assert result["hits"][0]["content_id"] == "trusted-lexical-hit"
    assert result["dense_enabled"] is False
    assert result["reranker_used"] is False
    assert "lexical_only" in result["degraded_reasons"]
    assert "online_worker_failed:RuntimeError" in result["degraded_reasons"]


def test_invalid_catalog_before_snapshot_is_rejected(
) -> None:
    args = argparse.Namespace(
        query="hello", top_k=8, no_rerank=False, timeout=2.0, _test_worker_mode="invalid"
    )

    with pytest.raises(ValueError, match="catalog seal mismatch"):
        knowledge_cli._query_generation_hard_deadline(
            args,
            "gen-a",
            deadline=time.monotonic() + 2.0,
            start_method=_safe_test_process_method(),
            _worker_target=_deadline_test_worker,
        )


def test_hard_deadline_uses_catalog_only_reader_when_full_manifest_context_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        knowledge_cli,
        "load_manifest",
        lambda _path: (_ for _ in ()).throw(
            ValueError("generation manifest lacks reader context")
        ),
    )
    args = argparse.Namespace(
        index_root=tmp_path,
        generation="gen-a",
        query="deadline phrase",
        top_k=8,
        no_rerank=False,
        timeout=1.0,
        _test_worker_mode="lexical",
    )

    result = knowledge_cli._query_generation_hard_deadline(
        args,
        "gen-a",
        deadline=time.monotonic() + 1.0,
        start_method=_safe_test_process_method(),
        _worker_target=_deadline_test_worker,
    )

    assert result["hits"][0]["content_id"] == "trusted-lexical-hit"
    assert result["dense_enabled"] is False
    assert result["reranker_used"] is False
    assert "lexical_only" in result["degraded_reasons"]


def test_manifest_mismatch_preflight_skips_full_reader_and_returns_lexical(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    generation = "manifest-mismatch"
    generation_dir = tmp_path / "generations" / generation
    generation_dir.mkdir(parents=True)
    manifest = {
        "schema_version": 2,
        "generation_id": generation,
        "status": "ready",
        "model_id": "embed-test",
        "model_revision": "embed-revision",
        "reranker_model_id": "rerank-test",
        "reranker_revision": "rerank-revision",
        "dimension": 3,
        "row_count": 1,
        "dtype": "float32",
        "normalized": True,
        "query_template": "query-mutated",
        "document_template": "document-v1",
        "chunker_version": "chunker-v1",
        "payload_version": "text-v1",
        "hard_max_tokens": 800,
        "catalog_hash": "a" * 64,
        "vectors_hash": "b" * 64,
        "manifest_hash": "0" * 64,
    }
    (generation_dir / "manifest.json").write_text(
        json.dumps(manifest) + "\n", encoding="utf-8"
    )
    (generation_dir / "vectors.f32").write_bytes(b"\0" * 12)

    args = argparse.Namespace(
        index_root=tmp_path,
        query="deadline phrase",
        top_k=8,
        no_rerank=False,
        timeout=1.0,
        _test_worker_mode="lexical",
    )
    assert "manifest_hash_mismatch" in knowledge_cli._lexical_only_preflight_reasons(
        args, generation
    )

    started = time.monotonic()
    result = knowledge_cli._query_generation_hard_deadline(
        args,
        generation,
        deadline=time.monotonic() + 1.0,
        start_method=_safe_test_process_method(),
        _worker_target=_deadline_test_worker,
    )

    assert time.monotonic() - started < 0.5
    assert result["hits"][0]["content_id"] == "trusted-lexical-hit"
    assert result["dense_enabled"] is False
    assert result["reranker_used"] is False


def test_online_retrieval_requires_config_and_explicit_cli_flags(tmp_path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        """
sources: {wechat: {groups: [test]}}
semantic_features: {knowledge_retrieval_enabled: false}
models:
  summary: {endpoint: "http://summary", model: "summary", api_key_env: "K"}
telegram: {bot_token_env: "TT", chat_id_env: "TC"}
""",
        encoding="utf-8",
    )
    args = argparse.Namespace(enable_retrieval=False, config=config, index_root=tmp_path / "index")
    with pytest.raises(ValueError, match="--enable-retrieval"):
        knowledge_cli._require_knowledge_retrieval(args)
    args.enable_retrieval = True
    with pytest.raises(ValueError, match="feature flag is disabled"):
        knowledge_cli._require_knowledge_retrieval(args)
    config.write_text(
        config.read_text(encoding="utf-8").replace("enabled: false", "enabled: true"),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="requires an open release state"):
        knowledge_cli._require_knowledge_retrieval(args)

    (args.index_root).mkdir()
    (args.index_root / knowledge_cli.RETRIEVAL_KILL_SWITCH).write_text(
        '{"disabled":true}\n', encoding="utf-8"
    )
    with pytest.raises(ValueError, match="emergency kill switch"):
        knowledge_cli._require_knowledge_retrieval(args)


def test_regular_retrieval_requires_open_release_state_and_exact_pointer_pair(
    monkeypatch, tmp_path: Path
) -> None:
    config = tmp_path / "config.yaml"
    _write_semantic_config(config, regular=True)
    index_root = tmp_path / "index"
    index_root.mkdir()
    state_path = index_root / knowledge_cli.RELEASE_STATE_FILENAME
    knowledge_cli.prepare_release(
        state_path,
        candidate_generation="gen-b",
        baseline_generation="gen-a",
        candidate_artifact_sha256="c" * 64,
        baseline_artifact_sha256="d" * 64,
        evaluation_hash="a" * 64,
        shadow_journal_hash="b" * 64,
        prepared_at="2026-08-25T10:00:00+00:00",
    )
    monkeypatch.setattr(
        knowledge_cli, "resolve_current_generation", lambda _root: "gen-b"
    )
    monkeypatch.setattr(
        knowledge_cli, "read_pointer", lambda _root, _name: "gen-a"
    )
    args = argparse.Namespace(
        enable_retrieval=True,
        config=config,
        index_root=index_root,
    )

    with pytest.raises(ValueError, match="requires an open release"):
        knowledge_cli._require_knowledge_retrieval(args)

    knowledge_cli.promote_release(
        state_path,
        generation_id="gen-b",
        promoted_at="2026-08-25T10:05:00+00:00",
    )
    release = knowledge_cli._require_knowledge_retrieval(args)
    assert release is not None
    assert release["status"] == "open"

    monkeypatch.setattr(
        knowledge_cli, "resolve_current_generation", lambda _root: "other-generation"
    )
    with pytest.raises(ValueError, match="does not match CURRENT/PREVIOUS"):
        knowledge_cli._require_knowledge_retrieval(args)


def test_regular_query_rejects_explicit_non_current_generation(
    monkeypatch, tmp_path: Path
) -> None:
    queried = False
    monkeypatch.setattr(
        knowledge_cli,
        "_require_knowledge_retrieval",
        lambda _args: {"status": "open", "candidate_generation": "gen-current"},
    )
    monkeypatch.setattr(
        knowledge_cli, "resolve_current_generation", lambda _root: "gen-current"
    )

    def query(*_args, **_kwargs):
        nonlocal queried
        queried = True
        return {}

    monkeypatch.setattr(knowledge_cli, "_query_generation_hard_deadline", query)
    args = argparse.Namespace(
        generation="gen-old",
        index_root=tmp_path,
        timeout=8.0,
        query="hello",
    )

    with pytest.raises(ValueError, match="must match CURRENT"):
        knowledge_cli.cmd_query(args)
    assert queried is False


def test_regular_query_falls_back_to_release_bound_previous_when_current_untrusted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    monkeypatch.setattr(
        knowledge_cli,
        "_require_knowledge_retrieval",
        lambda _args: {
            "status": knowledge_cli.RELEASE_OPEN,
            "candidate_generation": "gen-current",
            "baseline_generation": "gen-baseline",
            "candidate_artifact_sha256": "c" * 64,
            "baseline_artifact_sha256": "d" * 64,
        },
    )
    monkeypatch.setattr(
        knowledge_cli, "_current_pointer_for_query", lambda _root: "gen-current"
    )
    monkeypatch.setattr(
        knowledge_cli,
        "read_pointer",
        lambda _root, name: "gen-baseline" if name == "PREVIOUS" else "gen-current",
    )

    def bind(_root, generation, _artifact):
        if generation == "gen-current":
            raise ValueError("malformed candidate manifest")
        return _stub_context("gen-baseline"), None

    queried: list[str] = []
    monkeypatch.setattr(knowledge_cli, "_bind_release_generation", bind)
    monkeypatch.setattr(
        knowledge_cli,
        "_query_generation_hard_deadline",
        lambda _args, generation, **_kwargs: queried.append(generation)
        or {
            "generation_id": generation,
            "dense_enabled": True,
            "degraded": False,
            "degraded_reasons": [],
            "reranker_used": True,
            "hits": [{"content_id": "baseline-hit"}],
        },
    )
    appended = False

    def append(*_args, **_kwargs):
        nonlocal appended
        appended = True

    monkeypatch.setattr(knowledge_cli, "append_shadow_event", append)
    args = argparse.Namespace(
        generation=None,
        index_root=tmp_path,
        timeout=8.0,
        query="hello",
        request_id="stable-request",
        journal=tmp_path / "shadow.jsonl",
    )

    assert knowledge_cli.cmd_query(args) == 0
    output = json.loads(capsys.readouterr().out)

    assert queried == ["gen-baseline"]
    assert output["served_generation"] == "gen-baseline"
    assert output["degraded"] is True
    assert "generation_fallback:ValueError" in output["degraded_reasons"]
    assert output["telemetry_recorded"] is False
    assert output["telemetry_error"] == "BaselineFallback"
    assert appended is False


def test_open_current_query_always_records_authoritative_guard_telemetry(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    monkeypatch.setattr(
        knowledge_cli,
        "_require_knowledge_retrieval",
        lambda _args: {
            "status": knowledge_cli.RELEASE_OPEN,
            "candidate_generation": "gen-current",
            "candidate_artifact_sha256": "c" * 64,
        },
    )
    monkeypatch.setattr(
        knowledge_cli, "resolve_current_generation", lambda _root: "gen-current"
    )
    monkeypatch.setattr(
        knowledge_cli,
        "_query_generation_hard_deadline",
        lambda _args, generation, **_kwargs: {
            "generation_id": generation,
            "reranker_used": True,
            "degraded_reasons": [],
            "hits": [],
        },
    )
    journal = tmp_path / "shadow/events.jsonl"
    args = argparse.Namespace(
        generation=None,
        index_root=tmp_path,
        timeout=8.0,
        query="hello",
        request_id="stable-request",
        no_record=True,
        journal=journal,
    )

    assert knowledge_cli.cmd_query(args) == 0
    output = json.loads(capsys.readouterr().out)
    event = json.loads(journal.read_text(encoding="utf-8"))

    assert output["telemetry_recorded"] is True
    assert event["generation_id"] == "gen-current"
    assert event["selected_candidate"] is True
    assert event["served_route"] == "candidate"
    assert event["reranker_attempted"] is True
    assert event["reranker_error"] is False
    assert event["request_hash"] == knowledge_cli.hashlib.sha256(
        b"stable-request"
    ).hexdigest()


def test_diagnostic_query_is_explicitly_non_production_and_never_records(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    monkeypatch.setattr(
        knowledge_cli,
        "_query_generation_hard_deadline",
        lambda _args, generation, **_kwargs: {"generation_id": generation, "hits": []},
    )
    args = argparse.Namespace(
        generation="gen-old",
        index_root=tmp_path,
        timeout=8.0,
        query="hello",
    )

    assert knowledge_cli.cmd_diagnostic_query(args) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["diagnostic"] is True
    assert output["production_authorized"] is False
    assert output["telemetry_recorded"] is False
    assert output["result"]["generation_id"] == "gen-old"


def test_regular_kill_switch_takes_priority_over_pending_release(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config.yaml"
    _write_semantic_config(config, regular=True)
    index_root = tmp_path / "index"
    index_root.mkdir()
    knowledge_cli.prepare_release(
        index_root / knowledge_cli.RELEASE_STATE_FILENAME,
        candidate_generation="gen-b",
        baseline_generation="gen-a",
        candidate_artifact_sha256="c" * 64,
        baseline_artifact_sha256="d" * 64,
        evaluation_hash="a" * 64,
        shadow_journal_hash="b" * 64,
        prepared_at="2026-08-25T10:00:00+00:00",
    )
    (index_root / knowledge_cli.RETRIEVAL_KILL_SWITCH).write_text(
        '{"disabled":true}\n', encoding="utf-8"
    )
    args = argparse.Namespace(
        enable_retrieval=True,
        config=config,
        index_root=index_root,
    )

    with pytest.raises(ValueError, match="emergency kill switch"):
        knowledge_cli._require_knowledge_retrieval(args)


def test_canary_requires_independent_flag_and_exact_pending_pointer_binding(
    monkeypatch, tmp_path: Path
) -> None:
    config = tmp_path / "config.yaml"
    _write_semantic_config(config, canary=False)
    index_root = tmp_path / "index"
    index_root.mkdir()
    state_path = index_root / knowledge_cli.RELEASE_STATE_FILENAME
    knowledge_cli.prepare_release(
        state_path,
        candidate_generation="gen-b",
        baseline_generation="gen-a",
        candidate_artifact_sha256="c" * 64,
        baseline_artifact_sha256="d" * 64,
        evaluation_hash="a" * 64,
        shadow_journal_hash="b" * 64,
        prepared_at="2026-08-25T10:00:00+00:00",
    )
    monkeypatch.setattr(
        knowledge_cli, "resolve_current_generation", lambda _root: "gen-b"
    )
    monkeypatch.setattr(
        knowledge_cli, "read_pointer", lambda _root, _name: "gen-a"
    )
    args = argparse.Namespace(
        enable_retrieval=True,
        config=config,
        index_root=index_root,
        candidate="gen-b",
        baseline=None,
    )

    with pytest.raises(ValueError, match="canary feature flag"):
        knowledge_cli._require_knowledge_retrieval(args, canary=True)

    _write_semantic_config(config, canary=True)
    # A prior incident latch blocks regular traffic, but exact isolated canary
    # traffic remains available so this release can prove it is safe.
    (index_root / knowledge_cli.RETRIEVAL_KILL_SWITCH).write_text(
        '{"disabled":true}\n', encoding="utf-8"
    )
    release = knowledge_cli._require_knowledge_retrieval(args, canary=True)
    assert release is not None
    assert release["status"] == "canary_pending"

    args.candidate = "other-generation"
    with pytest.raises(ValueError, match="must match release state and CURRENT"):
        knowledge_cli._require_knowledge_retrieval(args, canary=True)
    args.candidate = "gen-b"
    args.baseline = "other-baseline"
    with pytest.raises(ValueError, match="explicit canary baseline"):
        knowledge_cli._require_knowledge_retrieval(args, canary=True)

    args.baseline = None
    knowledge_cli.promote_release(
        state_path,
        generation_id="gen-b",
        promoted_at="2026-08-25T10:05:00+00:00",
    )
    with pytest.raises(ValueError, match="canary_pending"):
        knowledge_cli._require_knowledge_retrieval(args, canary=True)


def test_guard_execute_is_latched_for_one_release_and_allows_later_release(
    monkeypatch, tmp_path: Path
) -> None:
    state = {"CURRENT": "bad-generation", "PREVIOUS": "safe-generation"}
    calls: list[tuple[str, str]] = []

    monkeypatch.setattr(
        knowledge_cli,
        "resolve_current_generation",
        lambda _root: state["CURRENT"],
    )
    monkeypatch.setattr(
        knowledge_cli,
        "read_pointer",
        lambda _root, name: state[name],
    )
    monkeypatch.setattr(
        knowledge_cli,
        "read_raw_pointer",
        lambda _root, name: state[name],
    )

    def rollback(_root, *, failed_generation_id, safe_generation_id):
        assert failed_generation_id == state["CURRENT"]
        assert safe_generation_id == state["PREVIOUS"]
        calls.append((state["CURRENT"], state["PREVIOUS"]))
        state["CURRENT"], state["PREVIOUS"] = state["PREVIOUS"], state["CURRENT"]
        return {
            "status": "rolled_back",
            "generation_id": state["CURRENT"],
            "previous": state["PREVIOUS"],
        }

    monkeypatch.setattr(knowledge_cli, "rollback_generation", rollback)
    metrics = {"generation": {"id": "bad-generation"}}

    first = knowledge_cli._execute_guard_rollback(
        tmp_path,
        metrics,
        ["generation_invalid"],
    )
    second = knowledge_cli._execute_guard_rollback(
        tmp_path,
        {"generation": {"id": "bad-generation"}, "changed": True},
        ["generation_invalid", "incorrect_source_link"],
    )

    assert first["safe_generation"] == "safe-generation"
    assert second["status"] == "already_rolled_back"
    assert calls == [("bad-generation", "safe-generation")]
    assert state["CURRENT"] == "safe-generation"
    kill = json.loads((tmp_path / knowledge_cli.RETRIEVAL_KILL_SWITCH).read_text(encoding="utf-8"))
    assert kill["disabled"] is True

    state.update({"CURRENT": "later-bad", "PREVIOUS": "safe-generation"})
    with pytest.raises(ValueError, match="explicitly cleared"):
        knowledge_cli._execute_guard_rollback(
            tmp_path,
            {"generation": {"id": "later-bad"}},
            ["manifest_invalid"],
        )

    # Only an explicit operator action clears the emergency latch.  A genuinely
    # new release pair can then own one new incident and one new rollback.
    (tmp_path / knowledge_cli.RETRIEVAL_KILL_SWITCH).unlink()
    third = knowledge_cli._execute_guard_rollback(
        tmp_path,
        {"generation": {"id": "later-bad"}},
        ["manifest_invalid"],
    )
    fourth = knowledge_cli._execute_guard_rollback(
        tmp_path,
        {"generation": {"id": "later-bad"}, "changed": True},
        ["manifest_invalid", "incorrect_source_link"],
    )

    assert third["status"] == "rolled_back"
    assert fourth["status"] == "already_rolled_back"
    assert calls == [
        ("bad-generation", "safe-generation"),
        ("later-bad", "safe-generation"),
    ]
    assert state["CURRENT"] == "safe-generation"
    incidents = tmp_path / knowledge_cli.ROLLBACK_INCIDENT_DIR
    assert {path.name for path in incidents.iterdir()} == {
        "bad-generation.json",
        "later-bad.json",
    }


def test_guard_rejects_stale_generation_without_matching_incident(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        knowledge_cli,
        "resolve_current_generation",
        lambda _root: "current-generation",
    )
    monkeypatch.setattr(
        knowledge_cli,
        "read_pointer",
        lambda _root, _name: "previous-generation",
    )
    monkeypatch.setattr(
        knowledge_cli,
        "read_raw_pointer",
        lambda _root, _name: "previous-generation",
    )

    with pytest.raises(ValueError, match="no longer target CURRENT"):
        knowledge_cli._execute_guard_rollback(
            tmp_path,
            {"generation": {"id": "stale-generation"}},
            ["manifest_invalid"],
        )

    assert not (tmp_path / knowledge_cli.RETRIEVAL_KILL_SWITCH).exists()
    assert not (tmp_path / knowledge_cli.ROLLBACK_INCIDENT_DIR).exists()


def test_guard_migrates_matching_legacy_incident_without_deleting_it(
    monkeypatch, tmp_path: Path
) -> None:
    state = {"CURRENT": "safe-generation", "PREVIOUS": "failed-generation"}
    monkeypatch.setattr(
        knowledge_cli,
        "resolve_current_generation",
        lambda _root: state["CURRENT"],
    )
    monkeypatch.setattr(
        knowledge_cli,
        "read_pointer",
        lambda _root, name: state[name],
    )
    monkeypatch.setattr(
        knowledge_cli,
        "read_raw_pointer",
        lambda _root, name: state[name],
    )
    legacy_path = tmp_path / knowledge_cli.ROLLBACK_INCIDENT
    legacy = {
        "schema": "chatdaily-knowledge-rollback-incident.v1",
        "incident_id": "legacy-incident",
        "metrics_hash": "d" * 64,
        "failed_generation": "failed-generation",
        "safe_generation": "safe-generation",
        "reasons": ["manifest_invalid"],
        "status": "complete",
        "prepared_at": "2026-08-26T00:00:00+00:00",
        "completed_at": "2026-08-26T00:00:01+00:00",
    }
    knowledge_cli._durable_json(legacy_path, legacy)

    result = knowledge_cli._execute_guard_rollback(
        tmp_path,
        {"generation": {"id": "failed-generation"}},
        ["manifest_invalid"],
    )

    assert result["status"] == "already_rolled_back"
    migrated = tmp_path / knowledge_cli.ROLLBACK_INCIDENT_DIR / "failed-generation.json"
    assert json.loads(migrated.read_text(encoding="utf-8")) == legacy
    assert json.loads(legacy_path.read_text(encoding="utf-8")) == legacy
    assert (tmp_path / knowledge_cli.RETRIEVAL_KILL_SWITCH).is_file()


def test_guard_binds_raw_corrupt_current_and_rolls_back_to_complete_previous(
    tmp_path: Path,
) -> None:
    from chat_daily_tg.knowledge_index import (
        GenerationBuilder,
        SourceDocument,
        activate_generation,
        read_pointer,
    )

    class Counter:
        def count(self, text: str) -> int:
            return max(1, len(text.split()))

        def windows(self, text: str, **_kwargs) -> list[str]:
            return [text]

    class Client:
        embedding_model = "embed-test"
        embedding_revision = "embed-revision"
        reranker_model = "rerank-test"
        reranker_revision = "rerank-revision"
        dimension = 3
        batch_size = 2

        def health(self):
            return {"ready": True, "embedding": {"ready": True}}

        def embed_documents(self, texts):
            return np.asarray([[1.0, 0.0, 0.0] for _ in texts], dtype=np.float32)

    config = knowledge_cli.GenerationConfig(
        model_id="embed-test",
        model_revision="embed-revision",
        reranker_model_id="rerank-test",
        reranker_revision="rerank-revision",
        dimension=3,
    )
    for generation in ("guard-safe", "guard-corrupt"):
        report = GenerationBuilder(tmp_path, config, Counter(), Client()).build(
            [
                SourceDocument(
                    content_id=generation,
                    source_kind="article",
                    source_ref=f"source:{generation}",
                    text="guard vector",
                )
            ],
            generation_id=generation,
        )
        assert report["ok"] is True
    activate_generation(tmp_path, "guard-safe", expected=config)
    activate_generation(tmp_path, "guard-corrupt", expected=config)
    corrupt_vectors = tmp_path / "generations/guard-corrupt/vectors.f32"
    corrupt_vectors.write_bytes(b"corrupt")

    binding = knowledge_cli._generation_guard_binding(tmp_path, config)
    assert binding["id"] == "guard-corrupt"
    assert binding["previous_id"] == "guard-safe"
    assert binding["manifest_valid"] is False
    assert "vectors_hash_mismatch" in binding["verification_errors"]

    result = knowledge_cli._execute_guard_rollback(
        tmp_path,
        {"generation": {"id": "guard-corrupt"}},
        ["manifest_invalid"],
    )
    assert result["status"] == "rolled_back"
    assert read_pointer(tmp_path, "CURRENT") == "guard-safe"
    assert read_pointer(tmp_path, "PREVIOUS") == "guard-corrupt"
    assert corrupt_vectors.read_bytes() == b"corrupt"
    assert (
        tmp_path
        / knowledge_cli.ROLLBACK_INCIDENT_DIR
        / "guard-corrupt.json"
    ).is_file()


def test_guard_snapshot_and_execute_reach_missing_current_directory(
    tmp_path: Path,
) -> None:
    from chat_daily_tg.knowledge_index import (
        GenerationBuilder,
        SourceDocument,
        activate_generation,
        read_pointer,
        read_raw_pointer,
    )

    class Counter:
        def count(self, text: str) -> int:
            return max(1, len(text.split()))

        def windows(self, text: str, **_kwargs) -> list[str]:
            return [text]

    class Client:
        embedding_model = "embed-test"
        embedding_revision = "embed-revision"
        reranker_model = "rerank-test"
        reranker_revision = "rerank-revision"
        dimension = 3
        batch_size = 2

        def health(self):
            return {"ready": True, "embedding": {"ready": True}}

        def embed_documents(self, texts):
            return np.asarray([[1.0, 0.0, 0.0] for _ in texts], dtype=np.float32)

    root = tmp_path / "index"
    config = knowledge_cli.GenerationConfig(
        model_id="embed-test",
        model_revision="embed-revision",
        reranker_model_id="rerank-test",
        reranker_revision="rerank-revision",
        dimension=3,
    )
    report = GenerationBuilder(root, config, Counter(), Client()).build(
        [
            SourceDocument(
                content_id="guard-safe",
                source_kind="article",
                source_ref="source:guard-safe",
                text="guard safe vector",
            )
        ],
        generation_id="guard-safe",
    )
    assert report["ok"] is True
    activate_generation(root, "guard-safe", expected=config)
    (root / "PREVIOUS").write_text("guard-safe\n", encoding="utf-8")
    (root / "CURRENT").write_text("guard-missing\n", encoding="utf-8")
    assert not (root / "generations/guard-missing").exists()
    assert read_raw_pointer(root, "CURRENT") == "guard-missing"
    with pytest.raises(ValueError, match="pointer target is missing"):
        read_pointer(root, "CURRENT")

    now = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)
    runtime = tmp_path / "runtime.json"
    runtime.write_text('{"runtime":"fixture"}\n', encoding="utf-8")

    def evaluation(generation: str, recall: float) -> dict:
        return {
            "schema": "chatdaily-knowledge-evaluation.v1",
            "created_at": now.isoformat(),
            "generation_id": generation,
            "gold_set_hash": "a" * 64,
            "metrics": {
                "query_count": 200,
                "recall_at_50": recall,
                "stratum_ndcg_at_5": {"article": recall},
            },
        }

    current_evaluation = tmp_path / "missing-current-evaluation.json"
    baseline_evaluation = tmp_path / "safe-baseline-evaluation.json"
    current_evaluation.write_text(
        json.dumps(evaluation("guard-missing", 0.90)), encoding="utf-8"
    )
    baseline_evaluation.write_text(
        json.dumps(evaluation("guard-safe", 0.99)), encoding="utf-8"
    )
    journal = tmp_path / "missing-shadow.jsonl"
    rows = [
        {
            "kind": "query",
            "generation_id": "guard-missing",
            "timestamp": (now - timedelta(minutes=minutes)).isoformat(),
            "route": "candidate",
            "request_hash": f"{index:064x}",
            "latency_ms": 1.0,
            "reranker_attempted": True,
            "reranker_error": False,
        }
        for index, minutes in enumerate((25, 15, 5), 1)
    ]
    rows.append(
        {
            "kind": "source_link",
            "generation_id": "guard-missing",
            "timestamp": (now - timedelta(minutes=1)).isoformat(),
            "accurate": True,
        }
    )
    journal.write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )

    metrics = knowledge_cli._compose_guard_snapshot(
        index_root=root,
        expected=config,
        runtime_config_path=runtime,
        current_evaluation_path=current_evaluation,
        baseline_evaluation_path=baseline_evaluation,
        shadow_journal_path=journal,
        generated_at=now,
        max_evaluation_age_seconds=86400.0,
    )
    assert metrics["generation"]["id"] == "guard-missing"
    assert metrics["generation"]["manifest_valid"] is False
    assert "manifest_unsafe_or_missing" in metrics["generation"]["verification_errors"]
    assert "manifest_invalid" in knowledge_cli.rollback_reasons(metrics)

    result = knowledge_cli._execute_guard_rollback(
        root,
        metrics,
        knowledge_cli.rollback_reasons(metrics),
    )
    assert result["safe_generation"] == "guard-safe"
    assert read_pointer(root, "CURRENT") == "guard-safe"
    assert read_raw_pointer(root, "PREVIOUS") == "guard-missing"
    assert not (root / "generations/guard-missing").exists()


def _guard_snapshot_fixture(monkeypatch, tmp_path: Path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    now = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)
    current_generation = "current-generation"
    baseline_generation = "baseline-generation"
    runtime = tmp_path / "runtime.json"
    runtime.write_text('{"runtime":"fixture"}\n', encoding="utf-8")
    current_evaluation = tmp_path / "current-evaluation.json"
    baseline_evaluation = tmp_path / "baseline-evaluation.json"

    def evaluation(generation: str, recall: float, ndcg: float) -> dict:
        return {
            "schema": "chatdaily-knowledge-evaluation.v1",
            "created_at": now.isoformat(),
            "generation_id": generation,
            "gold_set_hash": "a" * 64,
            "metrics": {
                "query_count": 200,
                "recall_at_50": recall,
                "stratum_ndcg_at_5": {"telegram": ndcg, "video": ndcg},
            },
        }

    current_evaluation.write_text(
        json.dumps(evaluation(current_generation, 0.94, 0.84)),
        encoding="utf-8",
    )
    baseline_evaluation.write_text(
        json.dumps(evaluation(baseline_generation, 0.98, 0.91)),
        encoding="utf-8",
    )
    journal = tmp_path / "shadow.jsonl"
    rows = []
    for index, minutes_ago in enumerate((25, 15, 5), 1):
        rows.append(
            {
                "schema": "chatdaily-knowledge-shadow.v1",
                "kind": "query",
                "generation_id": current_generation,
                "timestamp": (now - timedelta(minutes=minutes_ago)).isoformat(),
                "route": "candidate",
                "request_hash": f"{index:064x}",
                "latency_ms": 9000.0,
                "reranker_attempted": True,
                "reranker_error": index == 3,
                "generation_context": _stub_context(current_generation),
            }
        )
    rows.append(
        {
            "schema": "chatdaily-knowledge-shadow.v1",
            "kind": "source_link",
            "generation_id": current_generation,
            "timestamp": (now - timedelta(minutes=1)).isoformat(),
            "accurate": True,
            "generation_context": _stub_context(current_generation),
        }
    )
    journal.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )
    expected = knowledge_cli.GenerationConfig(
        model_revision="embedding-revision",
        reranker_revision="reranker-revision",
    )
    binding = {
        "id": current_generation,
        "model_id": expected.model_id,
        "model_revision": expected.model_revision,
        "reranker_model_id": expected.reranker_model_id,
        "reranker_revision": expected.reranker_revision,
        "dimension": expected.dimension,
        "manifest_sha256": "b" * 64,
        "identity_valid": True,
        "manifest_valid": True,
        "coverage": 1.0,
    }
    live_binding = dict(binding)
    monkeypatch.setattr(
        knowledge_cli,
        "_generation_guard_binding",
        lambda _root, _expected: dict(live_binding),
    )
    monkeypatch.setattr(
        knowledge_cli,
        "read_pointer",
        lambda _root, name: baseline_generation if name == "PREVIOUS" else None,
    )
    monkeypatch.setattr(
        knowledge_cli,
        "_require_guard_evaluation_artifact",
        lambda _report, _directory, generation_id, _label: {
            "generation_id": generation_id,
            "manifest_hash": "e" * 64,
            "catalog_hash": "f" * 64,
            "vectors_hash": "0" * 64,
        },
    )

    def compose():
        return knowledge_cli._compose_guard_snapshot(
            index_root=tmp_path / "index",
            expected=expected,
            runtime_config_path=runtime,
            current_evaluation_path=current_evaluation,
            baseline_evaluation_path=baseline_evaluation,
            shadow_journal_path=journal,
            generated_at=now,
            max_evaluation_age_seconds=86400.0,
        )

    def validate(metrics, *, current_time=now):
        return knowledge_cli.validate_guard_metrics(
            metrics,
            index_root=tmp_path / "index",
            expected=expected,
            runtime_config_path=runtime,
            now=current_time,
            max_age_seconds=1200.0,
            max_evaluation_age_seconds=86400.0,
        )

    return {
        "now": now,
        "runtime": runtime,
        "current_evaluation": current_evaluation,
        "baseline_evaluation": baseline_evaluation,
        "journal": journal,
        "expected": expected,
        "binding": live_binding,
        "compose": compose,
        "validate": validate,
    }


def test_guard_snapshot_is_complete_and_bound_to_authoritative_inputs(
    monkeypatch, tmp_path: Path
) -> None:
    fixture = _guard_snapshot_fixture(monkeypatch, tmp_path)
    metrics = fixture["compose"]()

    assert fixture["validate"](metrics) == metrics
    assert metrics["schema"] == knowledge_cli.GUARD_METRICS_SCHEMA
    assert metrics["generation"]["id"] == "current-generation"
    assert metrics["evaluation"]["recall_at_50"]["drop"] == pytest.approx(0.04)
    assert metrics["evaluation"]["stratum_ndcg_at_5"]["max_drop"] == pytest.approx(0.07)
    assert metrics["runtime"]["reranker"] == {
        "start": (fixture["now"] - timedelta(minutes=10)).isoformat(),
        "end": fixture["now"].isoformat(),
        "request_count": 1,
        "error_count": 1,
        "error_rate": 1.0,
    }
    assert [window["sample_count"] for window in metrics["runtime"]["p95_windows"]] == [
        1,
        1,
        1,
    ]
    assert set(metrics["inputs"]) == {
        "runtime_config",
        "current_evaluation",
        "baseline_evaluation",
        "shadow_journal",
    }


@pytest.mark.parametrize("label", ["current", "baseline"])
def test_guard_evaluation_binding_allows_status_only_manifest_change(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, label: str
) -> None:
    sealed = {
        "schema": "chatdaily-knowledge-generation-artifact.v1",
        "generation_id": "same-generation",
        "manifest_sha256": "a" * 64,
        "manifest_hash": "b" * 64,
        "catalog_hash": "c" * 64,
        "vectors_hash": "d" * 64,
    }
    live = {**sealed, "manifest_sha256": "e" * 64}
    monkeypatch.setattr(
        knowledge_cli,
        "generation_artifact_identity",
        lambda *_args, **_kwargs: live,
    )

    assert knowledge_cli._require_guard_evaluation_artifact(
        {"artifact_identity": sealed},
        tmp_path / "same-generation",
        "same-generation",
        label,
    ) == {
        "generation_id": "same-generation",
        "manifest_hash": "b" * 64,
        "catalog_hash": "c" * 64,
        "vectors_hash": "d" * 64,
    }


@pytest.mark.parametrize("label", ["current", "baseline"])
@pytest.mark.parametrize("field", ["manifest_hash", "catalog_hash", "vectors_hash"])
def test_guard_evaluation_binding_rejects_same_id_artifact_rebuild(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    label: str,
    field: str,
) -> None:
    sealed = {
        "schema": "chatdaily-knowledge-generation-artifact.v1",
        "generation_id": "same-generation",
        "manifest_sha256": "a" * 64,
        "manifest_hash": "b" * 64,
        "catalog_hash": "c" * 64,
        "vectors_hash": "d" * 64,
    }
    live = dict(sealed)
    live[field] = "e" * 64
    monkeypatch.setattr(
        knowledge_cli,
        "generation_artifact_identity",
        lambda *_args, **_kwargs: live,
    )

    with pytest.raises(ValueError, match=f"{label} guard evaluation artifact changed"):
        knowledge_cli._require_guard_evaluation_artifact(
            {"artifact_identity": sealed},
            tmp_path / "same-generation",
            "same-generation",
            label,
        )


def test_guard_snapshot_rejects_shadow_from_same_id_rebuilt_artifact(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fixture = _guard_snapshot_fixture(monkeypatch, tmp_path)
    old_context = _stub_context("current-generation")
    old_context["artifact_sha256"] = "a" * 64
    rows = [
        {**json.loads(line), "generation_context": old_context}
        for line in fixture["journal"].read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    fixture["journal"].write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="no candidate query samples"):
        fixture["compose"]()


@pytest.mark.parametrize(
    ("mutate", "error"),
    [
        (lambda value: value["generation"].pop("manifest_sha256"), "authoritative inputs"),
        (
            lambda value: value["runtime"]["reranker"].__setitem__("request_count", True),
            "authoritative inputs",
        ),
        (
            lambda value: value["verification"].__setitem__("coverage", float("nan")),
            "non-finite",
        ),
        (
            lambda value: value["runtime"]["p95_windows"][0].__setitem__("p95_ms", float("inf")),
            "non-finite",
        ),
    ],
)
def test_guard_snapshot_rejects_missing_wrong_type_and_nonfinite_values(
    monkeypatch, tmp_path: Path, mutate, error: str
) -> None:
    fixture = _guard_snapshot_fixture(monkeypatch, tmp_path)
    metrics = copy.deepcopy(fixture["compose"]())
    mutate(metrics)

    with pytest.raises(ValueError, match=error):
        fixture["validate"](metrics)


def test_guard_snapshot_rejects_stale_changed_and_cross_generation_inputs(
    monkeypatch, tmp_path: Path
) -> None:
    fixture = _guard_snapshot_fixture(monkeypatch, tmp_path)
    metrics = fixture["compose"]()

    with pytest.raises(ValueError, match="stale"):
        fixture["validate"](
            metrics,
            current_time=fixture["now"] + timedelta(seconds=1201),
        )

    fixture["current_evaluation"].write_text(
        fixture["current_evaluation"].read_text(encoding="utf-8") + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="authoritative inputs"):
        fixture["validate"](metrics)

    fixture = _guard_snapshot_fixture(monkeypatch, tmp_path / "cross-generation")
    metrics = fixture["compose"]()
    fixture["binding"]["manifest_sha256"] = "c" * 64
    with pytest.raises(ValueError, match="authoritative inputs"):
        fixture["validate"](metrics)


def test_guard_snapshot_requires_current_and_previous_evaluation_bindings(
    monkeypatch, tmp_path: Path
) -> None:
    fixture = _guard_snapshot_fixture(monkeypatch, tmp_path)
    report = json.loads(fixture["current_evaluation"].read_text(encoding="utf-8"))
    report["generation_id"] = "not-current"
    fixture["current_evaluation"].write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(ValueError, match="not bound to CURRENT"):
        fixture["compose"]()

    fixture = _guard_snapshot_fixture(monkeypatch, tmp_path / "previous")
    report = json.loads(fixture["baseline_evaluation"].read_text(encoding="utf-8"))
    report["generation_id"] = "not-previous"
    fixture["baseline_evaluation"].write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(ValueError, match="not bound to PREVIOUS"):
        fixture["compose"]()


def test_guard_validation_fails_before_rollback_mutation(monkeypatch, tmp_path: Path) -> None:
    metrics = tmp_path / "metrics.json"
    metrics.write_text('{"schema":"wrong"}\n', encoding="utf-8")
    executed = False

    monkeypatch.setattr(
        knowledge_cli,
        "_runtime_generation_config",
        lambda _args: (knowledge_cli.GenerationConfig(), {}, tmp_path),
    )

    def execute(*_args, **_kwargs):
        nonlocal executed
        executed = True
        raise AssertionError("rollback must not run")

    monkeypatch.setattr(knowledge_cli, "_execute_guard_rollback", execute)
    args = argparse.Namespace(
        metrics=metrics,
        index_root=tmp_path / "index",
        runtime_config=tmp_path / "runtime.json",
        max_age_seconds=1200.0,
        max_evaluation_age_seconds=86400.0,
        execute=True,
    )

    with pytest.raises(ValueError, match="schema"):
        knowledge_cli.cmd_guard(args)
    assert executed is False


def test_durable_json_overwrites_regular_file_and_rejects_symlink(
    monkeypatch, tmp_path: Path
) -> None:
    output = tmp_path / "guard" / "metrics.json"
    knowledge_cli._durable_json(output, {"value": 1})
    real_write = knowledge_cli.os.write

    def partial_write(descriptor, payload):
        return real_write(descriptor, payload[: max(1, len(payload) // 2)])

    monkeypatch.setattr(knowledge_cli.os, "write", partial_write)
    knowledge_cli._durable_json(output, {"value": 2, "payload": "x" * 100})
    monkeypatch.setattr(knowledge_cli.os, "write", real_write)
    assert json.loads(output.read_text(encoding="utf-8")) == {
        "value": 2,
        "payload": "x" * 100,
    }

    outside = tmp_path / "outside.json"
    outside.write_text('{"value":0}\n', encoding="utf-8")
    output.unlink()
    output.symlink_to(outside)
    with pytest.raises(ValueError, match="unsafe guard state path"):
        knowledge_cli._durable_json(output, {"value": 3})
    assert json.loads(outside.read_text(encoding="utf-8")) == {"value": 0}


def test_runtime_objects_fingerprint_embedding_and_reranker_independently(
    monkeypatch, tmp_path
) -> None:
    embedding_path = tmp_path / "embedding"
    reranker_path = tmp_path / "reranker"
    embedding_path.mkdir()
    reranker_path.mkdir()
    runtime = tmp_path / "runtime.json"
    runtime.write_text(
        json.dumps(
            {
                "embedding_path": str(embedding_path),
                "reranker_path": str(reranker_path),
                "embedding_model": "embed-model",
                "reranker_model": "rerank-model",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        knowledge_cli,
        "model_revision_fingerprint",
        lambda path: f"fingerprint:{Path(path).name}",
    )
    monkeypatch.setattr(knowledge_cli, "TokenCounter", lambda path: ("counter", path))
    args = argparse.Namespace(
        runtime_config=runtime,
        model_revision="",
        reranker_revision="",
        dimension=4096,
        endpoint=None,
        batch_size=16,
        timeout=8.0,
    )

    config, _counter, client = knowledge_cli._runtime_objects(args)

    assert config.model_revision == "fingerprint:embedding"
    assert config.reranker_revision == "fingerprint:reranker"
    assert client.embedding_revision == config.model_revision
    assert client.reranker_revision == config.reranker_revision


def test_canary_query_routes_selected_request_to_candidate(monkeypatch, tmp_path, capsys) -> None:
    readers: list[_Reader] = []

    def reader(args: argparse.Namespace) -> _Reader:
        instance = _Reader(args.generation)
        readers.append(instance)
        return instance

    monkeypatch.setattr(knowledge_cli, "canary_selected", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        knowledge_cli, "resolve_current_generation", lambda *args, **kwargs: "gen-b"
    )
    monkeypatch.setattr(knowledge_cli, "_reader", reader)
    monkeypatch.setattr(
        knowledge_cli,
        "_query_generation_hard_deadline",
        lambda args, generation, **_kwargs: _direct_query(reader, args, generation),
    )
    monkeypatch.setattr(
        knowledge_cli,
        "_require_knowledge_retrieval",
        lambda _args, **_kwargs: _pending_release(),
    )
    args = argparse.Namespace(
        request_id="request-1",
        candidate="gen-b",
        percent=10.0,
        index_root=tmp_path,
        query="hello",
        top_k=8,
        no_rerank=False,
        record=False,
        journal=None,
    )

    assert knowledge_cli.cmd_canary_query(args) == 0
    output = json.loads(capsys.readouterr().out)

    assert output["selected"] is True
    assert output["route"] == "candidate"
    assert output["served_generation"] == "gen-b"
    assert readers[0].closed is True


def test_canary_query_falls_back_to_current_when_candidate_is_invalid(
    monkeypatch, tmp_path, capsys
) -> None:
    readers: list[_Reader] = []

    def reader(args: argparse.Namespace) -> _Reader:
        instance = _Reader(args.generation, fail=args.generation == "gen-b")
        readers.append(instance)
        return instance

    monkeypatch.setattr(knowledge_cli, "canary_selected", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        knowledge_cli, "resolve_current_generation", lambda *args, **kwargs: "gen-b"
    )
    monkeypatch.setattr(knowledge_cli, "_reader", reader)
    monkeypatch.setattr(
        knowledge_cli,
        "_query_generation_hard_deadline",
        lambda args, generation, **_kwargs: _direct_query(reader, args, generation),
    )
    monkeypatch.setattr(
        knowledge_cli,
        "_require_knowledge_retrieval",
        lambda _args, **_kwargs: _pending_release(),
    )
    args = argparse.Namespace(
        request_id="request-1",
        candidate="gen-b",
        percent=10.0,
        index_root=tmp_path,
        query="hello",
        top_k=8,
        no_rerank=False,
        record=False,
        journal=None,
    )

    assert knowledge_cli.cmd_canary_query(args) == 0
    output = json.loads(capsys.readouterr().out)

    assert output["selected"] is True
    assert output["route"] == "baseline"
    assert output["served_generation"] == "gen-a"
    assert output["fallback_reason"].startswith("ValueError:")
    assert all(reader.closed for reader in readers)


def test_canary_skips_candidate_with_degraded_manifest_context_and_does_not_record(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    monkeypatch.setattr(knowledge_cli, "canary_selected", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        knowledge_cli,
        "_require_knowledge_retrieval",
        lambda _args, **_kwargs: _pending_release(),
    )
    monkeypatch.setattr(
        knowledge_cli, "_current_pointer_for_query", lambda _root: "gen-b"
    )

    def bind(_root, generation, _artifact):
        if generation == "gen-b":
            return None, "generation_context_invalid:ValueError"
        return _stub_context("gen-a"), None

    calls: list[str] = []
    monkeypatch.setattr(knowledge_cli, "_bind_release_generation", bind)
    monkeypatch.setattr(
        knowledge_cli,
        "_query_generation_hard_deadline",
        lambda _args, generation, **_kwargs: calls.append(generation)
        or {"generation_id": generation, "hits": [], "degraded_reasons": []},
    )
    appended = False

    def append(*_args, **_kwargs):
        nonlocal appended
        appended = True

    monkeypatch.setattr(knowledge_cli, "append_shadow_event", append)
    args = argparse.Namespace(
        request_id="request-1",
        candidate="gen-b",
        percent=10.0,
        index_root=tmp_path,
        query="hello",
        top_k=8,
        no_rerank=False,
        timeout=8.0,
        record=True,
        journal=tmp_path / "shadow.jsonl",
    )

    assert knowledge_cli.cmd_canary_query(args) == 0
    output = json.loads(capsys.readouterr().out)

    assert calls == ["gen-a"]
    assert output["route"] == "baseline"
    assert output["served_generation"] == "gen-a"
    assert output["fallback_reason"] == "generation_context_invalid:ValueError"
    assert output["recorded"] is False
    assert output["record_error"] == "candidate_generation_context_invalid"
    assert appended is False


def test_post_activation_canary_uses_previous_as_baseline(monkeypatch, tmp_path, capsys) -> None:
    readers: list[_Reader] = []

    def pointer(root, name: str) -> str:
        assert name == "PREVIOUS"
        return "gen-a"

    def reader(args: argparse.Namespace) -> _Reader:
        instance = _Reader(args.generation)
        readers.append(instance)
        return instance

    monkeypatch.setattr(knowledge_cli, "canary_selected", lambda *args, **kwargs: False)
    monkeypatch.setattr(knowledge_cli, "read_pointer", pointer)
    monkeypatch.setattr(
        knowledge_cli, "resolve_current_generation", lambda *args, **kwargs: "gen-b"
    )
    monkeypatch.setattr(knowledge_cli, "_reader", reader)
    monkeypatch.setattr(
        knowledge_cli,
        "_query_generation_hard_deadline",
        lambda args, generation, **_kwargs: _direct_query(reader, args, generation),
    )
    monkeypatch.setattr(
        knowledge_cli,
        "_require_knowledge_retrieval",
        lambda _args, **_kwargs: _pending_release(),
    )
    args = argparse.Namespace(
        request_id="request-1",
        candidate="gen-b",
        baseline=None,
        percent=10.0,
        index_root=tmp_path,
        query="hello",
        top_k=8,
        no_rerank=False,
        record=False,
        journal=None,
    )

    assert knowledge_cli.cmd_canary_query(args) == 0
    output = json.loads(capsys.readouterr().out)

    assert output["baseline_generation"] == "gen-a"
    assert output["served_generation"] == "gen-a"
    assert readers[0].generation == "gen-a"


@pytest.mark.parametrize("failure", [TimeoutError("deadline"), RuntimeError("worker")])
def test_canary_candidate_failures_share_one_deadline_and_record_selected_fallback(
    monkeypatch, tmp_path: Path, capsys, failure: Exception
) -> None:
    calls: list[tuple[str, float]] = []
    monkeypatch.setattr(knowledge_cli, "canary_selected", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        knowledge_cli,
        "_require_knowledge_retrieval",
        lambda _args, **_kwargs: _pending_release(),
    )
    monkeypatch.setattr(
        knowledge_cli, "resolve_current_generation", lambda _root: "gen-b"
    )
    monkeypatch.setattr(knowledge_cli.time, "monotonic", lambda: 100.0)

    def query(_args, generation: str, *, deadline: float):
        calls.append((generation, deadline))
        if generation == "gen-b":
            raise failure
        return {"generation_id": generation, "hits": [], "degraded_reasons": []}

    monkeypatch.setattr(knowledge_cli, "_query_generation_hard_deadline", query)
    journal = tmp_path / "shadow.jsonl"
    args = argparse.Namespace(
        request_id="request-1",
        candidate="gen-b",
        percent=10.0,
        index_root=tmp_path,
        query="hello",
        top_k=8,
        no_rerank=False,
        timeout=6.0,
        record=True,
        journal=journal,
    )

    assert knowledge_cli.cmd_canary_query(args) == 0
    output = json.loads(capsys.readouterr().out)
    event = json.loads(journal.read_text(encoding="utf-8"))

    assert calls == [("gen-b", 104.0), ("gen-a", 106.0)]
    assert output["served_generation"] == "gen-a"
    assert output["fallback_reason"].startswith(type(failure).__name__ + ":")
    assert event["selected_candidate"] is True
    assert event["served_route"] == "baseline"
    assert event["candidate_latency_ms"] <= event["total_latency_ms"]


def test_canary_baseline_failure_propagates_without_a_third_attempt(
    monkeypatch, tmp_path: Path
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(knowledge_cli, "canary_selected", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        knowledge_cli,
        "_require_knowledge_retrieval",
        lambda _args, **_kwargs: _pending_release(),
    )
    monkeypatch.setattr(
        knowledge_cli, "resolve_current_generation", lambda _root: "gen-b"
    )

    def query(_args, generation: str, *, deadline: float):
        del deadline
        calls.append(generation)
        if generation == "gen-b":
            raise RuntimeError("candidate failed")
        raise TimeoutError("baseline deadline")

    monkeypatch.setattr(knowledge_cli, "_query_generation_hard_deadline", query)
    args = argparse.Namespace(
        request_id="request-1",
        candidate="gen-b",
        percent=10.0,
        index_root=tmp_path,
        query="hello",
        top_k=8,
        no_rerank=False,
        timeout=8.0,
        record=False,
        journal=None,
    )

    with pytest.raises(TimeoutError, match="baseline deadline"):
        knowledge_cli.cmd_canary_query(args)
    assert calls == ["gen-b", "gen-a"]


def test_shadow_record_rejects_non_object(tmp_path) -> None:
    event = tmp_path / "event.json"
    event.write_text("[]\n", encoding="utf-8")
    args = argparse.Namespace(event=event, journal=tmp_path / "journal.jsonl", index_root=tmp_path)

    with pytest.raises(ValueError, match="one JSON object"):
        knowledge_cli.cmd_shadow_record(args)


def test_shadow_record_rejects_backfilled_timestamp(tmp_path) -> None:
    event = tmp_path / "event.json"
    event.write_text(
        json.dumps(
            {
                "kind": "health",
                "generation_id": "gen-a",
                "available": True,
                "timestamp": "2026-08-01T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    args = argparse.Namespace(event=event, journal=tmp_path / "journal.jsonl", index_root=tmp_path)

    with pytest.raises(ValueError, match="cannot be backfilled"):
        knowledge_cli.cmd_shadow_record(args)


@pytest.mark.parametrize(
    "kind",
    [
        "health",
        "query",
        "incremental",
        "incremental_refresh_receipt",
        "source_freshness",
        "source_link",
    ],
)
def test_shadow_record_rejects_manual_release_evidence(tmp_path, kind) -> None:
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"kind": kind}), encoding="utf-8")
    args = argparse.Namespace(
        event=event,
        journal=tmp_path / "journal.jsonl",
        index_root=tmp_path,
    )

    with pytest.raises(ValueError, match="authoritative command"):
        knowledge_cli.cmd_shadow_record(args)


def test_shadow_audit_sources_appends_only_derived_events(monkeypatch, tmp_path) -> None:
    snapshot = object()
    appended: list[dict] = []
    monkeypatch.setattr(knowledge_cli, "_source_paths", lambda _args: object())
    monkeypatch.setattr(knowledge_cli, "collect_sources", lambda _paths: snapshot)
    monkeypatch.setattr(
        knowledge_cli,
        "audit_incremental_freshness",
        lambda generation, value: {
            "kind": "source_freshness",
            "producer": "source-freshness.v1",
            "generation_id": generation.name,
            "success": True,
            "noop": True,
        },
    )
    monkeypatch.setattr(
        knowledge_cli,
        "audit_source_links",
        lambda generation, value: {
            "kind": "source_link",
            "generation_id": generation.name,
            "accurate": True,
        },
    )
    monkeypatch.setattr(
        knowledge_cli,
        "append_shadow_event",
        lambda _path, event: appended.append(dict(event)) or dict(event),
    )
    args = argparse.Namespace(
        index_root=tmp_path,
        generation="gen-a",
        journal=tmp_path / "events.jsonl",
    )

    assert knowledge_cli.cmd_shadow_audit_sources(args) == 0
    assert [event["kind"] for event in appended] == ["source_freshness", "source_link"]
    assert all(event["generation_id"] == "gen-a" for event in appended)


def _incremental_refresh_fixture(tmp_path: Path):
    from chat_daily_tg.knowledge_index import GenerationBuilder, SourceDocument, SourceLink
    from chat_daily_tg.knowledge_sources import SourceSnapshot

    class Counter:
        def count(self, text: str) -> int:
            return max(1, len(text.split()))

        def windows(self, text: str, **_kwargs) -> list[str]:
            return [text]

    class Client:
        embedding_model = "embed-test"
        embedding_revision = "embed-revision"
        reranker_model = "rerank-test"
        reranker_revision = "rerank-revision"
        dimension = 3
        batch_size = 2

        def __init__(self) -> None:
            self.document_payloads: list[str] = []

        def health(self):
            return {"ready": True, "embedding": {"ready": True}}

        def embed_documents(self, texts):
            self.document_payloads.extend(texts)
            return np.asarray([[1.0, 0.0, 0.0] for _ in texts], dtype=np.float32)

    config = knowledge_cli.GenerationConfig(
        model_id="embed-test",
        model_revision="embed-revision",
        reranker_model_id="rerank-test",
        reranker_revision="rerank-revision",
        dimension=3,
    )
    candidate_documents = (
        SourceDocument(
            content_id="unchanged",
            source_kind="podcast",
            source_ref="episode:unchanged",
            text="unchanged source text",
            producer="Podcast4Bot",
            authority="Podcast4Bot",
            mapping_status="confirmed",
            source_links=[
                SourceLink(
                    chat_id=10,
                    message_id=11,
                    ledger_schema="media-sent.v2",
                )
            ],
        ),
        SourceDocument(
            content_id="changed",
            source_kind="podcast",
            source_ref="episode:changed",
            text="old source text",
            producer="Podcast4Bot",
            authority="Podcast4Bot",
            mapping_status="confirmed",
            source_links=[
                SourceLink(
                    chat_id=10,
                    message_id=12,
                    ledger_schema="media-sent.v2",
                )
            ],
        ),
    )
    candidate_cursor = {"podcast": {"mode": "set", "count": 1}}
    GenerationBuilder(tmp_path, config, Counter(), Client()).build(
        candidate_documents,
        source_cursors=candidate_cursor,
        generation_id="shadow-candidate",
    )
    changed_snapshot = SourceSnapshot(
        documents=(
            candidate_documents[0],
            SourceDocument(
                content_id="changed",
                source_kind="podcast",
                source_ref="episode:changed",
                text="new source text",
                producer="Podcast4Bot",
                authority="Podcast4Bot",
                mapping_status="confirmed",
                source_links=list(candidate_documents[1].source_links),
            ),
        ),
        cursors={"podcast": {"mode": "set", "count": 2}},
        feedback_events=(),
    )
    noop_snapshot = SourceSnapshot(
        documents=candidate_documents,
        cursors=candidate_cursor,
        feedback_events=(),
    )
    return config, Counter(), Client(), changed_snapshot, noop_snapshot


def test_incremental_refresh_noop_records_only_freshness(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    config, counter, client, _changed, snapshot = _incremental_refresh_fixture(tmp_path)
    monkeypatch.setattr(
        knowledge_cli, "_generation_shadow_context", _REAL_GENERATION_SHADOW_CONTEXT
    )
    monkeypatch.setattr(
        knowledge_cli, "_runtime_objects", lambda _args: (config, counter, client)
    )
    monkeypatch.setattr(knowledge_cli, "_source_paths", lambda _args: object())
    monkeypatch.setattr(knowledge_cli, "collect_sources", lambda _paths: snapshot)
    journal = tmp_path / "shadow/events.jsonl"
    args = argparse.Namespace(
        index_root=tmp_path,
        shadow_candidate="shadow-candidate",
        output_generation="unused-output",
        journal=journal,
    )

    assert knowledge_cli.cmd_incremental_refresh(args) == 0
    result = json.loads(capsys.readouterr().out)
    events = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
    assert result["status"] == "noop"
    assert result["refresh_performed"] is False
    assert [event["kind"] for event in events] == ["source_freshness"]
    assert events[0]["success"] is events[0]["noop"] is True
    assert client.document_payloads == []
    assert not (tmp_path / "generations/unused-output").exists()


def test_incremental_refresh_rebuilds_all_rows_and_appends_sealed_receipt(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    config, counter, client, snapshot, _noop = _incremental_refresh_fixture(tmp_path)
    monkeypatch.setattr(
        knowledge_cli, "_generation_shadow_context", _REAL_GENERATION_SHADOW_CONTEXT
    )
    monkeypatch.setattr(
        knowledge_cli, "_runtime_objects", lambda _args: (config, counter, client)
    )
    monkeypatch.setattr(knowledge_cli, "_source_paths", lambda _args: object())
    monkeypatch.setattr(knowledge_cli, "collect_sources", lambda _paths: snapshot)
    candidate = tmp_path / "generations/shadow-candidate"
    baseline_hashes = {
        name: knowledge_cli.sha256_file(candidate / name)
        for name in ("manifest.json", "catalog.sqlite", "vectors.f32")
    }
    journal = tmp_path / "shadow/events.jsonl"
    args = argparse.Namespace(
        index_root=tmp_path,
        shadow_candidate="shadow-candidate",
        output_generation="incremental-output",
        journal=journal,
    )

    assert knowledge_cli.cmd_incremental_refresh(args) == 0
    result = json.loads(capsys.readouterr().out)
    events = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
    receipt = events[-1]
    output = tmp_path / "generations/incremental-output"
    assert result["status"] == "refreshed"
    assert [event["kind"] for event in events] == [
        "source_freshness",
        "incremental_refresh_receipt",
    ]
    assert receipt["success"] is receipt["refresh_performed"] is True
    assert receipt["noop"] is False
    assert receipt["generation_id"] == receipt["baseline_generation_id"] == "shadow-candidate"
    assert receipt["output_generation_id"] == "incremental-output"
    assert receipt["baseline_source_cursor_hash"] != receipt["source_snapshot_hash"]
    assert receipt["output_source_cursor_hash"] == receipt["source_snapshot_hash"]
    assert len(client.document_payloads) == 2
    assert any("unchanged source text" in payload for payload in client.document_payloads)
    assert any("new source text" in payload for payload in client.document_payloads)
    assert knowledge_cli.verify_generation(output, expected=config, full=True)["ok"] is True
    assert knowledge_cli.load_manifest(output)["baseline_pointer"] == "INCREMENTAL"
    assert {
        name: knowledge_cli.sha256_file(candidate / name)
        for name in ("manifest.json", "catalog.sqlite", "vectors.f32")
    } == baseline_hashes
    assert not (tmp_path / "CURRENT").exists()
    assert not (tmp_path / "PREVIOUS").exists()
    assert not (tmp_path / "SHADOW_CANDIDATE").exists()


def test_incremental_refresh_failure_never_appends_success_receipt(
    monkeypatch, tmp_path: Path
) -> None:
    config, counter, client, snapshot, _noop = _incremental_refresh_fixture(tmp_path)
    monkeypatch.setattr(
        knowledge_cli, "_generation_shadow_context", _REAL_GENERATION_SHADOW_CONTEXT
    )
    monkeypatch.setattr(
        knowledge_cli, "_runtime_objects", lambda _args: (config, counter, client)
    )
    monkeypatch.setattr(knowledge_cli, "_source_paths", lambda _args: object())
    monkeypatch.setattr(knowledge_cli, "collect_sources", lambda _paths: snapshot)
    monkeypatch.setattr(
        client,
        "embed_documents",
        lambda _texts: (_ for _ in ()).throw(ValueError("embedding failed")),
    )
    journal = tmp_path / "shadow/events.jsonl"
    args = argparse.Namespace(
        index_root=tmp_path,
        shadow_candidate="shadow-candidate",
        output_generation="failed-output",
        journal=journal,
    )

    with pytest.raises(ValueError, match="embedding failed"):
        knowledge_cli.cmd_incremental_refresh(args)
    events = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
    assert [event["kind"] for event in events] == ["source_freshness"]
    assert events[0]["success"] is True and events[0]["noop"] is False
    assert (tmp_path / "generations/failed-output").is_dir()


def test_incremental_refresh_rejects_corrupt_candidate_before_journaling(
    monkeypatch, tmp_path: Path
) -> None:
    config, counter, client, snapshot, _noop = _incremental_refresh_fixture(tmp_path)
    monkeypatch.setattr(
        knowledge_cli, "_generation_shadow_context", _REAL_GENERATION_SHADOW_CONTEXT
    )
    monkeypatch.setattr(
        knowledge_cli, "_runtime_objects", lambda _args: (config, counter, client)
    )
    monkeypatch.setattr(knowledge_cli, "_source_paths", lambda _args: object())
    monkeypatch.setattr(knowledge_cli, "collect_sources", lambda _paths: snapshot)
    candidate_vectors = tmp_path / "generations/shadow-candidate/vectors.f32"
    candidate_vectors.write_bytes(b"corrupt")
    journal = tmp_path / "shadow/events.jsonl"
    args = argparse.Namespace(
        index_root=tmp_path,
        shadow_candidate="shadow-candidate",
        output_generation="blocked-output",
        journal=journal,
    )

    with pytest.raises(ValueError, match="vectors_hash mismatch"):
        knowledge_cli.cmd_incremental_refresh(args)
    assert not journal.exists()
    assert not (tmp_path / "generations/blocked-output").exists()


def test_evaluate_uses_same_process_release_pair_when_baseline_is_given(
    monkeypatch, tmp_path
) -> None:
    readers: dict[str, object] = {}

    class Reader:
        def __init__(self, generation: str):
            self.generation = generation
            self.closed = False

        def close(self) -> None:
            self.closed = True

    def reader(args):
        value = Reader(args.generation)
        readers[args.generation] = value
        return value

    monkeypatch.setattr(knowledge_cli, "_reader", reader)
    monkeypatch.setattr(
        knowledge_cli,
        "evaluate_release_pair",
        lambda candidate, baseline, gold, allow_small=False: {
            "passed": True,
            "candidate": candidate.generation,
            "baseline": baseline.generation,
            "gold": str(gold),
            "allow_small": allow_small,
        },
    )
    args = argparse.Namespace(
        generation="candidate",
        baseline_generation="baseline",
        gold=tmp_path / "gold.jsonl",
        allow_small=False,
        baseline_e2e_p95_ms=None,
        output=None,
    )

    assert knowledge_cli.cmd_evaluate(args) == 0
    assert readers["candidate"].closed is True
    assert readers["baseline"].closed is True


def test_evaluate_uses_independent_baseline_runtime_context_and_rejects_degradation(
    monkeypatch, tmp_path: Path
) -> None:
    seen_runtime_configs: dict[str, Path] = {}

    class Reader:
        def __init__(self, generation: str, *, degraded: bool):
            self.generation = generation
            self.dense_enabled = not degraded
            self.degraded_reasons = ["query_model_revision_mismatch"] if degraded else []
            self.verification = {"ok": True}
            self.closed = False

        def close(self) -> None:
            self.closed = True

    readers: list[Reader] = []

    def reader(args):
        seen_runtime_configs[args.generation] = args.runtime_config
        value = Reader(args.generation, degraded=args.generation == "baseline")
        readers.append(value)
        return value

    monkeypatch.setattr(knowledge_cli, "_reader", reader)
    args = argparse.Namespace(
        generation="candidate",
        baseline_generation="baseline",
        runtime_config=tmp_path / "candidate-runtime.json",
        baseline_runtime_config=tmp_path / "baseline-runtime.json",
        endpoint=None,
        baseline_endpoint="http://baseline.test/v1",
        dimension=4096,
        baseline_dimension=2048,
        gold=tmp_path / "gold.jsonl",
        allow_small=False,
        baseline_e2e_p95_ms=None,
        output=None,
    )

    with pytest.raises(ValueError, match="baseline runtime context does not match"):
        knowledge_cli.cmd_evaluate(args)

    assert seen_runtime_configs == {
        "candidate": tmp_path / "candidate-runtime.json",
        "baseline": tmp_path / "baseline-runtime.json",
    }
    assert all(reader.closed for reader in readers)


def test_v1_manifest_context_is_used_for_canary_evaluate_and_diagnostic_readers(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    index_root = tmp_path / "index"
    runtime_path = tmp_path / "runtime.json"
    runtime_path.write_text(
        json.dumps(
            {
                "embedding_path": str(tmp_path / "embedding"),
                "reranker_path": str(tmp_path / "reranker"),
                "bind": "127.0.0.1",
                "port": 9999,
            }
        ),
        encoding="utf-8",
    )

    def write_manifest(generation: str, chunker: str) -> None:
        generation_dir = index_root / "generations" / generation
        generation_dir.mkdir(parents=True)
        identity = knowledge_cli.GenerationConfig(
            model_id="manifest-embed",
            model_revision="manifest-embed-revision",
            reranker_model_id="manifest-rerank",
            reranker_revision="manifest-rerank-revision",
            dimension=17,
            chunker_version=chunker,
        ).identity()
        (generation_dir / "manifest.json").write_text(
            json.dumps({"generation_id": generation, **identity}),
            encoding="utf-8",
        )

    write_manifest("gen-v2", "chatdaily-chunker-v2")
    write_manifest("gen-v1", "chatdaily-chunker-v1")
    contexts: list[tuple[str, knowledge_cli.GenerationConfig]] = []

    class Runtime:
        def __init__(self, _endpoint, **kwargs):
            self.embedding_model = kwargs["embedding_model"]
            self.embedding_revision = kwargs["embedding_revision"]
            self.reranker_model = kwargs["reranker_model"]
            self.reranker_revision = kwargs["reranker_revision"]
            self.dimension = kwargs["dimension"]

        def health(self):
            return {
                "ready": True,
                "embedding_revision": self.embedding_revision,
                "reranker_revision": self.reranker_revision,
            }

    class Reader:
        def __init__(
            self, generation_dir, client, _counter, *, expected, _online_state=None
        ):
            del _online_state
            self.generation = generation_dir.name
            self.manifest = {"generation_id": self.generation}
            self.client = client
            self.verification = {"ok": True}
            self.dense_enabled = True
            self.degraded_reasons = []
            self.closed = False
            contexts.append((self.generation, expected))

        def search(self, _query, **_kwargs):
            return {
                "generation_id": self.generation,
                "reranker_used": True,
                "degraded_reasons": [],
                "hits": [],
            }

        def close(self):
            self.closed = True

    monkeypatch.setattr(knowledge_cli, "TokenCounter", lambda _path: object())
    monkeypatch.setattr(knowledge_cli, "QwenRuntimeClient", Runtime)
    monkeypatch.setattr(knowledge_cli, "GenerationReader", Reader)

    def direct_query(args, generation):
        child_args = argparse.Namespace(**vars(args))
        child_args.generation = generation
        reader = knowledge_cli._reader(child_args)
        try:
            return reader.search(
                child_args.query,
                top_k=child_args.top_k,
                use_reranker=not child_args.no_rerank,
            )
        finally:
            reader.close()

    monkeypatch.setattr(
        knowledge_cli,
        "_query_generation_hard_deadline",
        lambda args, generation, **_kwargs: direct_query(args, generation),
    )

    common = {
        "index_root": index_root,
        "runtime_config": runtime_path,
        "endpoint": None,
        "model_revision": "caller-must-not-win",
        "reranker_revision": "caller-must-not-win",
        "dimension": 999,
        "batch_size": 2,
        "timeout": 8.0,
        "query": "hello",
        "top_k": 8,
        "no_rerank": False,
    }
    monkeypatch.setattr(knowledge_cli, "canary_selected", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        knowledge_cli,
        "_require_knowledge_retrieval",
        lambda _args, **_kwargs: _pending_release("gen-v2", "gen-v1"),
    )
    monkeypatch.setattr(
        knowledge_cli, "resolve_current_generation", lambda _root: "gen-v2"
    )
    canary_args = argparse.Namespace(
        **common,
        request_id="request-v1",
        candidate="gen-v2",
        baseline=None,
        percent=10.0,
        record=False,
        journal=None,
    )
    assert knowledge_cli.cmd_canary_query(canary_args) == 0
    assert json.loads(capsys.readouterr().out)["served_generation"] == "gen-v1"

    monkeypatch.setattr(
        knowledge_cli,
        "evaluate_release_pair",
        lambda candidate, baseline, _gold, **_kwargs: {
            "passed": True,
            "candidate": candidate.generation,
            "baseline": baseline.generation,
        },
    )
    evaluate_args = argparse.Namespace(
        **common,
        generation="gen-v2",
        baseline_generation="gen-v1",
        baseline_runtime_config=runtime_path,
        baseline_endpoint=None,
        baseline_dimension=999,
        gold=tmp_path / "gold.jsonl",
        allow_small=False,
        baseline_e2e_p95_ms=None,
        output=None,
    )
    assert knowledge_cli.cmd_evaluate(evaluate_args) == 0
    capsys.readouterr()

    diagnostic_args = argparse.Namespace(**common, generation="gen-v1")
    assert knowledge_cli.cmd_diagnostic_query(diagnostic_args) == 0
    diagnostic = json.loads(capsys.readouterr().out)
    assert diagnostic["production_authorized"] is False
    assert diagnostic["telemetry_recorded"] is False

    v1_contexts = [context for generation, context in contexts if generation == "gen-v1"]
    assert len(v1_contexts) == 3
    assert all(context.chunker_version == "chatdaily-chunker-v1" for context in v1_contexts)
    assert all(context.dimension == 17 for context in v1_contexts)
    assert all(context.model_revision == "manifest-embed-revision" for context in v1_contexts)
    assert all(context.reranker_revision == "manifest-rerank-revision" for context in v1_contexts)


def test_activation_requires_complete_seven_day_shadow(monkeypatch, tmp_path) -> None:
    evaluation = tmp_path / "evaluation.json"
    evaluation.write_text("{}\n", encoding="utf-8")
    shadow_journal = tmp_path / "shadow.jsonl"
    shadow_journal.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        knowledge_cli,
        "validate_evaluation_for_activation",
        lambda *args, **kwargs: {},
    )
    monkeypatch.setattr(
        knowledge_cli,
        "summarize_shadow",
        lambda *args, **kwargs: {
            "ready": False,
            "failure_reasons": ["shadow_below_7_days"],
        },
    )
    args = argparse.Namespace(
        evaluation=evaluation,
        generation="gen-a",
        shadow_journal=shadow_journal,
        index_root=tmp_path,
    )

    with pytest.raises(ValueError, match="complete seven-day shadow"):
        knowledge_cli.cmd_activate(args)


def test_activation_rejects_evaluation_for_a_different_switching_baseline(
    monkeypatch, tmp_path: Path
) -> None:
    args = _release_args(tmp_path)
    monkeypatch.setattr(
        knowledge_cli,
        "validate_evaluation_for_activation",
        lambda *_args, **_kwargs: {"baseline_generation_id": "gen-other"},
    )
    monkeypatch.setattr(
        knowledge_cli, "summarize_shadow", lambda *_args, **_kwargs: _ready_shadow()
    )
    monkeypatch.setattr(
        knowledge_cli,
        "_runtime_objects",
        lambda _args: (knowledge_cli.GenerationConfig(), None, None),
    )
    monkeypatch.setattr(
        knowledge_cli, "resolve_current_generation", lambda _root: "gen-a"
    )

    with pytest.raises(ValueError, match="evaluation baseline does not match"):
        knowledge_cli.cmd_activate(args)
    assert not (
        args.index_root / knowledge_cli.RELEASE_STATE_FILENAME
    ).exists()


def test_activation_prepares_hash_bound_release_before_pointer_switch(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    args = _release_args(tmp_path)
    calls: list[str] = []
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        knowledge_cli, "validate_evaluation_for_activation", lambda *_args, **_kwargs: {}
    )
    monkeypatch.setattr(
        knowledge_cli, "summarize_shadow", lambda *_args, **_kwargs: _ready_shadow()
    )
    monkeypatch.setattr(
        knowledge_cli,
        "_runtime_objects",
        lambda _args: (knowledge_cli.GenerationConfig(), None, None),
    )
    monkeypatch.setattr(
        knowledge_cli, "resolve_current_generation", lambda _root: "gen-a"
    )

    def prepare(_path, **kwargs):
        calls.append("prepare")
        captured.update(kwargs)
        return _pending_release()

    def activate(_root, generation: str, *, expected, baseline_generation_id):
        del expected
        calls.append("activate")
        captured["activation_baseline_generation_id"] = baseline_generation_id
        return {"status": "activated", "generation_id": generation}

    monkeypatch.setattr(knowledge_cli, "prepare_release", prepare)
    monkeypatch.setattr(knowledge_cli, "activate_generation", activate)

    assert knowledge_cli.cmd_activate(args) == 0
    output = json.loads(capsys.readouterr().out)

    assert calls == ["prepare", "activate"]
    assert captured["candidate_generation"] == "gen-b"
    assert captured["baseline_generation"] == "gen-a"
    assert captured["activation_baseline_generation_id"] == "gen-a"
    assert captured["evaluation_hash"] == knowledge_cli.sha256_file(args.evaluation)
    assert captured["shadow_journal_hash"] == knowledge_cli.sha256_file(args.shadow_journal)
    assert output["release_state"]["status"] == "canary_pending"


def test_activation_rechecks_evidence_after_release_preparation(
    monkeypatch, tmp_path: Path
) -> None:
    args = _release_args(tmp_path)
    activated = False
    monkeypatch.setattr(
        knowledge_cli, "validate_evaluation_for_activation", lambda *_args, **_kwargs: {}
    )
    monkeypatch.setattr(
        knowledge_cli, "summarize_shadow", lambda *_args, **_kwargs: _ready_shadow()
    )
    monkeypatch.setattr(
        knowledge_cli,
        "_runtime_objects",
        lambda _args: (knowledge_cli.GenerationConfig(), None, None),
    )
    monkeypatch.setattr(
        knowledge_cli, "resolve_current_generation", lambda _root: "gen-a"
    )

    def prepare(_path, **_kwargs):
        args.evaluation.write_text('{"changed":true}\n', encoding="utf-8")
        return _pending_release()

    def activate(*_args, **_kwargs):
        nonlocal activated
        activated = True
        return {}

    monkeypatch.setattr(knowledge_cli, "prepare_release", prepare)
    monkeypatch.setattr(knowledge_cli, "activate_generation", activate)

    with pytest.raises(ValueError, match="changed after release preparation"):
        knowledge_cli.cmd_activate(args)
    assert activated is False


def test_activation_rechecks_receipt_baseline_before_pointer_mutation(
    monkeypatch, tmp_path: Path
) -> None:
    args = _release_args(tmp_path)
    currents = iter(["gen-a", "gen-raced"])
    activated = False
    monkeypatch.setattr(
        knowledge_cli, "validate_evaluation_for_activation", lambda *_args, **_kwargs: {}
    )
    monkeypatch.setattr(
        knowledge_cli, "summarize_shadow", lambda *_args, **_kwargs: _ready_shadow()
    )
    monkeypatch.setattr(
        knowledge_cli,
        "_runtime_objects",
        lambda _args: (knowledge_cli.GenerationConfig(), None, None),
    )
    monkeypatch.setattr(
        knowledge_cli, "resolve_current_generation", lambda _root: next(currents)
    )

    def activate(*_args, **_kwargs):
        nonlocal activated
        activated = True
        return {}

    monkeypatch.setattr(knowledge_cli, "activate_generation", activate)

    with pytest.raises(ValueError, match="baseline changed before pointer mutation"):
        knowledge_cli.cmd_activate(args)
    assert activated is False


def test_failed_pointer_switch_leaves_pending_release_unauthorized(
    monkeypatch, tmp_path: Path
) -> None:
    args = _release_args(tmp_path)
    monkeypatch.setattr(
        knowledge_cli, "validate_evaluation_for_activation", lambda *_args, **_kwargs: {}
    )
    monkeypatch.setattr(
        knowledge_cli, "summarize_shadow", lambda *_args, **_kwargs: _ready_shadow()
    )
    monkeypatch.setattr(
        knowledge_cli,
        "_runtime_objects",
        lambda _args: (knowledge_cli.GenerationConfig(), None, None),
    )
    monkeypatch.setattr(
        knowledge_cli, "resolve_current_generation", lambda _root: "gen-a"
    )
    monkeypatch.setattr(
        knowledge_cli,
        "activate_generation",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("pointer switch failed")),
    )

    with pytest.raises(OSError, match="pointer switch failed"):
        knowledge_cli.cmd_activate(args)

    state = knowledge_cli.read_release_state(
        args.index_root / knowledge_cli.RELEASE_STATE_FILENAME
    )
    assert state is not None
    assert state["status"] == "canary_pending"
    assert state["candidate_generation"] == "gen-b"


@pytest.mark.parametrize(
    ("ready", "canary_ready"),
    [(False, True), (True, False)],
)
def test_promotion_requires_both_shadow_gates_and_preserves_pending(
    monkeypatch, tmp_path: Path, ready: bool, canary_ready: bool
) -> None:
    args = _release_args(tmp_path)
    knowledge_cli.prepare_release(
        args.index_root / knowledge_cli.RELEASE_STATE_FILENAME,
        candidate_generation="gen-b",
        baseline_generation="gen-a",
        candidate_artifact_sha256="c" * 64,
        baseline_artifact_sha256="d" * 64,
        evaluation_hash="a" * 64,
        shadow_journal_hash="b" * 64,
        prepared_at="2026-08-25T10:00:00+00:00",
    )
    monkeypatch.setattr(
        knowledge_cli, "resolve_current_generation", lambda _root: "gen-b"
    )
    monkeypatch.setattr(knowledge_cli, "read_pointer", lambda _root, _name: "gen-a")
    monkeypatch.setattr(
        knowledge_cli,
        "summarize_shadow",
        lambda *_args, **_kwargs: {
            "ready": ready,
            "failure_reasons": [] if ready else ["shadow_not_ready"],
            "canary_ready": canary_ready,
            "canary_failure_reasons": [] if canary_ready else ["canary_not_ready"],
        },
    )

    with pytest.raises(ValueError, match="ready shadow and canary"):
        knowledge_cli.cmd_promote(args)
    state = knowledge_cli.read_release_state(
        args.index_root / knowledge_cli.RELEASE_STATE_FILENAME
    )
    assert state is not None and state["status"] == "canary_pending"


def test_promotion_opens_exact_current_previous_release(monkeypatch, tmp_path: Path, capsys) -> None:
    args = _release_args(tmp_path)
    state_path = args.index_root / knowledge_cli.RELEASE_STATE_FILENAME
    knowledge_cli.prepare_release(
        state_path,
        candidate_generation="gen-b",
        baseline_generation="gen-a",
        candidate_artifact_sha256="c" * 64,
        baseline_artifact_sha256="d" * 64,
        evaluation_hash="a" * 64,
        shadow_journal_hash="b" * 64,
        prepared_at="2026-08-25T10:00:00+00:00",
    )
    monkeypatch.setattr(
        knowledge_cli, "resolve_current_generation", lambda _root: "gen-b"
    )
    monkeypatch.setattr(knowledge_cli, "read_pointer", lambda _root, _name: "gen-a")
    monkeypatch.setattr(
        knowledge_cli, "summarize_shadow", lambda *_args, **_kwargs: _ready_shadow()
    )

    assert knowledge_cli.cmd_promote(args) == 0
    output = json.loads(capsys.readouterr().out)
    state = knowledge_cli.read_release_state(state_path)

    assert state is not None and state["status"] == "open"
    assert output["release_state"] == state
    assert output["kill_switch"]["status"] == "absent"


def test_status_reports_release_state(monkeypatch, tmp_path: Path, capsys) -> None:
    index_root = tmp_path / "index"
    index_root.mkdir()
    state = knowledge_cli.prepare_release(
        index_root / knowledge_cli.RELEASE_STATE_FILENAME,
        candidate_generation="gen-b",
        baseline_generation="gen-a",
        candidate_artifact_sha256="c" * 64,
        baseline_artifact_sha256="d" * 64,
        evaluation_hash="a" * 64,
        shadow_journal_hash="b" * 64,
        prepared_at="2026-08-25T10:00:00+00:00",
    )
    monkeypatch.setattr(
        knowledge_cli, "resolve_current_generation", lambda _root: "gen-b"
    )
    monkeypatch.setattr(knowledge_cli, "read_pointer", lambda _root, _name: "gen-a")

    assert knowledge_cli.cmd_status(argparse.Namespace(index_root=index_root)) == 0
    output = json.loads(capsys.readouterr().out)

    assert output["release_state"] == state


def test_shadow_probe_records_runtime_readiness(monkeypatch, tmp_path, capsys) -> None:
    runtime = tmp_path / "runtime.json"
    runtime.write_text(
        json.dumps(
            {
                "bind": "127.0.0.1",
                "port": 8790,
                "embedding_model": "embed-test",
                "reranker_model": "rerank-test",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    def get(url: str, **kwargs) -> httpx.Response:
        request = httpx.Request("GET", url)
        return httpx.Response(
            200,
            request=request,
            json={
                "ready": True,
                "embedding": {"ready": True},
                "reranker": {"ready": True},
                "embedding_revision": "embed-revision",
                "reranker_revision": "rerank-revision",
            },
        )

    monkeypatch.setattr(knowledge_cli.httpx, "get", get)
    args = argparse.Namespace(
        runtime_config=runtime,
        endpoint=None,
        timeout=5.0,
        generation="gen-a",
        journal=tmp_path / "shadow.jsonl",
        index_root=tmp_path,
    )

    assert knowledge_cli.cmd_shadow_probe(args) == 0
    output = json.loads(capsys.readouterr().out)

    assert output["status"] == "available"
    assert output["event"]["embedding_ready"] is True
    assert output["event"]["reranker_ready"] is True


def test_shadow_probe_uses_runtime_token_without_recording_it(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    runtime = tmp_path / "runtime.json"
    runtime.write_text(
        json.dumps(
            {
                "bind": "127.0.0.1",
                "port": 8790,
                "embedding_model": "embed-test",
                "reranker_model": "rerank-test",
                "token_env": "SHADOW_PROBE_TEST_TOKEN",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SHADOW_PROBE_TEST_TOKEN", "test-secret")

    def get(url: str, **kwargs) -> httpx.Response:
        assert kwargs["headers"] == {"Authorization": "Bearer test-secret"}
        return httpx.Response(
            200,
            request=httpx.Request("GET", url),
            json={
                "ready": True,
                "embedding": {"ready": True},
                "reranker": {"ready": True},
                "embedding_revision": "embed-revision",
                "reranker_revision": "rerank-revision",
            },
        )

    monkeypatch.setattr(knowledge_cli.httpx, "get", get)
    journal = tmp_path / "shadow.jsonl"
    args = argparse.Namespace(
        runtime_config=runtime,
        endpoint=None,
        timeout=5.0,
        generation="gen-a",
        journal=journal,
        index_root=tmp_path,
    )

    assert knowledge_cli.cmd_shadow_probe(args) == 0
    output = capsys.readouterr().out
    assert "test-secret" not in output
    assert "test-secret" not in journal.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("runtime_override", "health_override", "failed_field"),
    [
        ({"embedding_model": "other-embed"}, {}, "runtime_config_identity_matches"),
        ({}, {"embedding_revision": "other-revision"}, "health_revision_matches"),
        ({}, {"reranker_model": "other-reranker"}, "runtime_identity_matches"),
    ],
)
def test_shadow_probe_fails_closed_on_runtime_context_mismatch(
    monkeypatch,
    tmp_path: Path,
    capsys,
    runtime_override: dict[str, str],
    health_override: dict[str, str],
    failed_field: str,
) -> None:
    runtime = {
        "bind": "127.0.0.1",
        "port": 8790,
        "embedding_model": "embed-test",
        "reranker_model": "rerank-test",
    }
    runtime.update(runtime_override)
    runtime_path = tmp_path / "runtime.json"
    runtime_path.write_text(json.dumps(runtime) + "\n", encoding="utf-8")
    health = {
        "ready": True,
        "embedding": {"ready": True},
        "reranker": {"ready": True},
        "embedding_revision": "embed-revision",
        "reranker_revision": "rerank-revision",
    }
    health.update(health_override)

    def get(url: str, **kwargs) -> httpx.Response:
        return httpx.Response(200, request=httpx.Request("GET", url), json=health)

    monkeypatch.setattr(knowledge_cli.httpx, "get", get)
    args = argparse.Namespace(
        runtime_config=runtime_path,
        endpoint=None,
        timeout=5.0,
        generation="gen-a",
        journal=tmp_path / "shadow.jsonl",
        index_root=tmp_path,
    )

    assert knowledge_cli.cmd_shadow_probe(args) == 2
    event = json.loads(capsys.readouterr().out)["event"]
    assert event["available"] is False
    assert event["error"] == "runtime_identity_mismatch"
    assert event[failed_field] is False
