"""Operator CLI for the rebuildable ChatDaily KnowledgeIndex."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import multiprocessing
import os
import re
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from multiprocessing.connection import wait as wait_connections
from pathlib import Path
from typing import Any, Sequence

import httpx
import yaml

from chat_daily_tg.config import Config

from chat_daily_tg.knowledge_eval import (
    evaluation_regression_metrics,
    evaluate_gold_set,
    evaluate_release_pair,
    generation_artifact_identity,
    validate_evaluation_for_activation,
)
from chat_daily_tg.knowledge_index import (
    DIMENSION,
    EMBEDDING_MODEL,
    ONLINE_QUERY_TIMEOUT,
    RERANKER_MODEL,
    SCHEMA_VERSION,
    GenerationBuilder,
    GenerationConfig,
    GenerationReader,
    LexicalGenerationReader,
    PrecomputedQueryEmbedding,
    QwenRuntimeClient,
    QwenRuntimeError,
    TokenCounter,
    activate_generation,
    bootstrap_generation,
    compute_manifest_hash,
    load_manifest,
    model_revision_fingerprint,
    read_pointer,
    read_raw_pointer,
    resolve_current_generation,
    render_query,
    rollback_generation,
    rollback_reasons,
    sha256_file,
    verify_generation,
    _open_online_verified_generation,
)
from chat_daily_tg.knowledge_release import (
    CANARY_PENDING,
    OPEN as RELEASE_OPEN,
    RELEASE_STATE_FILENAME,
    prepare_release,
    promote_release,
    read_release_state,
)
from chat_daily_tg.knowledge_sources import SourcePaths, collect_sources
from chat_daily_tg.knowledge_shadow_producers import (
    audit_incremental_freshness,
    audit_source_links,
)
from chat_daily_tg.knowledge_shadow import (
    GENERATION_CONTEXT_SCHEMA,
    INCREMENTAL_REFRESH_PRODUCER,
    append_shadow_event,
    canary_selected,
    guard_shadow_metrics,
    summarize_shadow,
    utc_now as shadow_utc_now,
    validate_generation_context,
)


DEFAULT_RUNTIME_CONFIG = Path(
    "~/Library/Application Support/HermesQwenVLRuntime/config/runtime.json"
).expanduser()
RETRIEVAL_KILL_SWITCH = "RETRIEVAL_DISABLED"
ROLLBACK_INCIDENT = ".rollback-incident.json"
ROLLBACK_INCIDENT_DIR = Path("guard/incidents")
GUARD_METRICS_SCHEMA = "chatdaily-knowledge-guard-metrics.v1"
GUARD_METRICS_MAX_AGE_SECONDS = 20 * 60
GUARD_EVALUATION_MAX_AGE_SECONDS = 24 * 60 * 60
GUARD_WINDOW_SECONDS = 10 * 60
GUARD_SOURCE_LINK_WINDOW_SECONDS = 7 * 24 * 60 * 60


def _json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _release_state_path(index_root: Path) -> Path:
    return Path(index_root).expanduser() / RELEASE_STATE_FILENAME


def _read_index_release_state(index_root: Path) -> dict[str, Any] | None:
    root = Path(index_root).expanduser()
    if not root.exists() and not root.is_symlink():
        return None
    if root.is_symlink() or not root.is_dir():
        raise ValueError("knowledge index root must be a regular non-symlink directory")
    return read_release_state(_release_state_path(root))


def _current_pointer_for_query(index_root: Path) -> str:
    """Resolve a healthy CURRENT, or read it without bypassing a pending switch."""

    root = Path(index_root).expanduser()
    try:
        return resolve_current_generation(root)
    except ValueError:
        switch_journal = root / ".switch-journal.json"
        if switch_journal.exists() or switch_journal.is_symlink():
            raise ValueError("knowledge query cannot bypass a pending generation switch")
        return read_pointer(root, "CURRENT")


def _require_knowledge_retrieval(
    args: argparse.Namespace, *, canary: bool = False
) -> dict[str, Any]:
    """Fail closed at the independent regular/canary release boundaries."""

    if getattr(args, "enable_retrieval", False) is not True:
        raise ValueError("knowledge retrieval requires explicit --enable-retrieval")
    config_path = Path(args.config).expanduser()
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("knowledge retrieval config must be a YAML object")
    config = Config(**raw)
    index_root = Path(
        getattr(args, "index_root", Path("~/chat-daily/index").expanduser())
    ).expanduser()
    kill_switch = index_root / RETRIEVAL_KILL_SWITCH

    if canary:
        if config.semantic_features.knowledge_canary_enabled is not True:
            raise ValueError("knowledge canary feature flag is disabled in semantic_features")
        release = _read_index_release_state(index_root)
        if release is None or release["status"] != CANARY_PENDING:
            raise ValueError("knowledge canary requires a canary_pending release state")
        candidate = str(getattr(args, "candidate", "") or "")
        current = _current_pointer_for_query(index_root)
        if release["candidate_generation"] != candidate or candidate != current:
            raise ValueError("knowledge canary candidate must match release state and CURRENT")
        previous = read_pointer(index_root, "PREVIOUS")
        if release["baseline_generation"] != previous:
            raise ValueError("knowledge canary baseline must match release state and PREVIOUS")
        explicit_baseline = getattr(args, "baseline", None)
        if explicit_baseline and explicit_baseline != release["baseline_generation"]:
            raise ValueError("explicit canary baseline does not match release state")
        # A rollback kill switch may intentionally remain latched while a new
        # candidate receives isolated 10% canary traffic.  Exact release and
        # pointer binding above is the only exception; regular queries remain
        # blocked until a successful promotion proves it is safe to clear.
        return release

    if kill_switch.exists() or kill_switch.is_symlink():
        raise ValueError("knowledge retrieval emergency kill switch is active")
    if config.semantic_features.knowledge_retrieval_enabled is not True:
        raise ValueError("knowledge retrieval feature flag is disabled in semantic_features")
    release = _read_index_release_state(index_root)
    if release is None:
        raise ValueError("regular knowledge retrieval requires an open release state")
    if release["status"] != RELEASE_OPEN:
        raise ValueError("regular knowledge retrieval requires an open release state")
    current = _current_pointer_for_query(index_root)
    previous = read_pointer(index_root, "PREVIOUS")
    if (
        release["candidate_generation"] != current
        or release["baseline_generation"] != previous
    ):
        raise ValueError("open knowledge release state does not match CURRENT/PREVIOUS")
    return release


def _runtime_config(path: Path) -> dict[str, Any]:
    value = json.loads(path.expanduser().read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Qwen runtime config must be a JSON object")
    return value


def _runtime_generation_config(
    args: argparse.Namespace,
) -> tuple[GenerationConfig, dict[str, Any], Path]:
    runtime = _runtime_config(args.runtime_config)
    model_path = Path(runtime["embedding_path"]).expanduser()
    reranker_path = Path(runtime["reranker_path"]).expanduser()
    revision = args.model_revision or model_revision_fingerprint(model_path)
    reranker_revision = args.reranker_revision or model_revision_fingerprint(reranker_path)
    config = GenerationConfig(
        model_id=str(runtime.get("embedding_model") or EMBEDDING_MODEL),
        model_revision=revision,
        dimension=int(args.dimension),
        reranker_model_id=str(runtime.get("reranker_model") or RERANKER_MODEL),
        reranker_revision=reranker_revision,
    )
    return config, runtime, model_path


def _runtime_objects(
    args: argparse.Namespace,
) -> tuple[GenerationConfig, TokenCounter, QwenRuntimeClient]:
    config, runtime, model_path = _runtime_generation_config(args)
    counter = TokenCounter(model_path)
    token_env = str(runtime.get("token_env") or "QWEN_VL_RUNTIME_TOKEN")
    token = os.environ.get(token_env, "")
    endpoint = args.endpoint or (
        f"http://{runtime.get('bind', '127.0.0.1')}:{runtime.get('port', 8790)}/v1"
    )
    client = QwenRuntimeClient(
        endpoint,
        embedding_model=config.model_id,
        embedding_revision=config.model_revision,
        reranker_model=config.reranker_model_id,
        reranker_revision=config.reranker_revision,
        dimension=config.dimension,
        batch_size=int(args.batch_size),
        timeout=float(args.timeout),
        token=token,
    )
    return config, counter, client


def _source_paths(args: argparse.Namespace) -> SourcePaths:
    defaults = SourcePaths.defaults(args.data_root)
    return SourcePaths(
        archive=args.archive or defaults.archive,
        chat_db=args.chat_db or defaults.chat_db,
        sent_ledger=args.sent_ledger or defaults.sent_ledger,
        media_ledger=args.media_ledger or defaults.media_ledger,
        podcast_root=args.podcast_root or defaults.podcast_root,
        feedback_events=args.feedback or defaults.feedback_events,
        feedback_reclassifications=(
            args.feedback_reclassifications or defaults.feedback_reclassifications
        ),
    )


def cmd_scan(args: argparse.Namespace) -> int:
    snapshot = collect_sources(_source_paths(args))
    by_kind: dict[str, int] = {}
    for document in snapshot.documents:
        by_kind[document.source_kind] = by_kind.get(document.source_kind, 0) + 1
    _json(
        {
            "status": "ok",
            "read_only": True,
            "documents": len(snapshot.documents),
            "by_source_kind": by_kind,
            "feedback_events": len(snapshot.feedback_events),
            "cursors": snapshot.cursors,
        }
    )
    return 0


def cmd_build(args: argparse.Namespace) -> int:
    config, counter, client = _runtime_objects(args)
    snapshot = collect_sources(_source_paths(args))
    builder = GenerationBuilder(args.index_root, config, counter, client)
    report = builder.build(
        snapshot.documents,
        source_cursors=snapshot.cursors,
        feedback_events=snapshot.feedback_events,
        generation_id=args.generation,
        resume=args.resume,
    )
    _json(report)
    return 0


def _generation_dir(args: argparse.Namespace, *, pointer: str = "CURRENT") -> Path:
    if args.generation:
        generation = args.generation
    elif pointer == "CURRENT":
        generation = resolve_current_generation(args.index_root)
    else:
        generation = read_pointer(args.index_root, pointer)
    return args.index_root / "generations" / generation


def cmd_verify(args: argparse.Namespace) -> int:
    expected = None
    if not args.manifest_only:
        expected, _, _ = _runtime_objects(args)
    report = verify_generation(_generation_dir(args), expected=expected, full=args.full)
    _json(report)
    return 0 if report["ok"] else 2


def _reader(args: argparse.Namespace) -> GenerationReader:
    generation_dir = _generation_dir(args)
    if bool(getattr(args, "_lexical_only", False)):
        return LexicalGenerationReader(
            generation_dir,
            degraded_reasons=tuple(
                str(value)
                for value in getattr(args, "_lexical_degraded_reasons", ())
            ),
        )
    manifest, config = _reader_manifest_context(generation_dir, online_query=False)
    runtime = _runtime_config(args.runtime_config)
    model_path = Path(runtime["embedding_path"]).expanduser()
    online_state: tuple[dict[str, Any], dict[str, Any], sqlite3.Connection | None] | None = None
    try:
        with ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="knowledge-tokenizer"
        ) as pool:
            counter_future = pool.submit(TokenCounter, model_path)
            online_state = _open_online_verified_generation(generation_dir, expected=config)
            counter = counter_future.result()
        client = _reader_runtime_client(args, runtime, config)
        reader = GenerationReader(
            generation_dir,
            client,
            counter,
            expected=config,
            _online_state=online_state,
        )
    except BaseException:
        if online_state is not None and online_state[2] is not None:
            online_state[2].close()
        raise
    return reader


def _reader_manifest_context(
    generation_dir: Path, *, online_query: bool
) -> tuple[dict[str, Any], GenerationConfig]:
    manifest = load_manifest(generation_dir)
    fields = (
        "model_id",
        "model_revision",
        "dimension",
        "dtype",
        "normalized",
        "query_template",
        "document_template",
        "chunker_version",
        "payload_version",
        "reranker_model_id",
        "reranker_revision",
        "hard_max_tokens",
    )
    missing = [field for field in fields if field not in manifest]
    if missing:
        raise ValueError(
            "generation manifest lacks reader context: " + ",".join(sorted(missing))
        )
    config = GenerationConfig(**{field: manifest[field] for field in fields})
    if online_query:
        errors: list[str] = []
        if manifest.get("manifest_hash") != compute_manifest_hash(manifest):
            errors.append("manifest_hash_mismatch")
        if manifest.get("schema_version") != SCHEMA_VERSION:
            errors.append("schema_version_mismatch")
        if manifest.get("status") not in {"ready", "active", "retired"}:
            errors.append("generation_incomplete")
        if any(
            not isinstance(manifest.get(field), str) or not str(manifest[field]).strip()
            for field in (
                "model_id",
                "model_revision",
                "reranker_model_id",
                "reranker_revision",
                "query_template",
                "document_template",
                "chunker_version",
                "payload_version",
            )
        ):
            errors.append("manifest_context_invalid")
        if (
            manifest.get("dtype") != "float32"
            or manifest.get("normalized") is not True
            or type(manifest.get("dimension")) is not int
            or manifest["dimension"] <= 0
            or type(manifest.get("hard_max_tokens")) is not int
            or manifest["hard_max_tokens"] <= 0
            or type(manifest.get("row_count")) is not int
            or manifest["row_count"] <= 0
        ):
            errors.append("manifest_shape_invalid")
        for field in ("catalog_hash", "vectors_hash"):
            if re.fullmatch(r"[0-9a-f]{64}", str(manifest.get(field) or "")) is None:
                errors.append(f"{field}_invalid")
        if errors:
            raise ValueError(
                "generation manifest failed online query preflight: "
                + ",".join(dict.fromkeys(errors))
            )
    return manifest, config


def _reader_runtime_client(
    args: argparse.Namespace,
    runtime: dict[str, Any],
    config: GenerationConfig,
) -> QwenRuntimeClient:

    # Runtime paths/token/endpoint remain operator-owned, but model identity is
    # taken from the immutable generation manifest.  QwenRuntimeClient then
    # requires the server's health and inference responses to attest those
    # exact revisions; CLI --model-revision values cannot rewrite history.
    token_env = str(runtime.get("token_env") or "QWEN_VL_RUNTIME_TOKEN")
    token = os.environ.get(token_env, "")
    endpoint = args.endpoint or (
        f"http://{runtime.get('bind', '127.0.0.1')}:{runtime.get('port', 8790)}/v1"
    )
    return QwenRuntimeClient(
        endpoint,
        embedding_model=config.model_id,
        embedding_revision=config.model_revision,
        reranker_model=config.reranker_model_id,
        reranker_revision=config.reranker_revision,
        dimension=config.dimension,
        batch_size=int(args.batch_size),
        timeout=float(args.timeout),
        token=token,
    )


def _precompute_query_embedding(
    *,
    counter_ready: Future[Any],
    model_path: Path,
    client: QwenRuntimeClient,
    query: str,
    rendered_query: str,
    manifest: dict[str, Any],
    deadline: float,
    timeout: float,
) -> PrecomputedQueryEmbedding:
    try:
        counter = TokenCounter(model_path)
    except BaseException as exc:
        counter_ready.set_exception(exc)
        raise
    counter_ready.set_result(counter)
    started = time.perf_counter()
    vector = None
    error_type: str | None = None
    try:
        if counter.count(rendered_query) > manifest["hard_max_tokens"]:
            raise ValueError("query exceeds generation hard token max")
        remaining = min(timeout, deadline - time.monotonic())
        if remaining <= 0:
            raise TimeoutError("query embedding deadline exhausted before request")
        vector = client.embed_queries([rendered_query], timeout=remaining)[0]
    except Exception as exc:
        error_type = type(exc).__name__
    return PrecomputedQueryEmbedding(
        query=query,
        rendered_query=rendered_query,
        generation_id=manifest["generation_id"],
        manifest_hash=manifest["manifest_hash"],
        model_id=manifest["model_id"],
        model_revision=manifest["model_revision"],
        dimension=manifest["dimension"],
        query_template=manifest["query_template"],
        vector=vector,
        elapsed_ms=(time.perf_counter() - started) * 1000,
        error_type=error_type,
    )


def _query_reader(
    args: argparse.Namespace,
) -> tuple[GenerationReader, ThreadPoolExecutor, Future[PrecomputedQueryEmbedding]]:
    """Build a verified reader while speculatively embedding its one query."""

    generation_dir = _generation_dir(args)
    manifest, config = _reader_manifest_context(generation_dir, online_query=True)
    clean_query = str(args.query).strip()
    if not clean_query:
        raise ValueError("query must not be empty")
    rendered_query = render_query(clean_query, template=config.query_template)
    runtime = _runtime_config(args.runtime_config)
    model_path = Path(runtime["embedding_path"]).expanduser()
    client = _reader_runtime_client(args, runtime, config)
    online_state: tuple[dict[str, Any], dict[str, Any], sqlite3.Connection | None] | None = None
    pool = ThreadPoolExecutor(
        max_workers=1, thread_name_prefix="knowledge-query-init"
    )
    counter_ready: Future[Any] = Future()
    precomputed_future = pool.submit(
        _precompute_query_embedding,
        counter_ready=counter_ready,
        model_path=model_path,
        client=client,
        query=clean_query,
        rendered_query=rendered_query,
        manifest=manifest,
        deadline=float(
            getattr(
                args,
                "_query_deadline",
                time.monotonic() + min(ONLINE_QUERY_TIMEOUT, float(args.timeout)),
            )
        ),
        timeout=min(ONLINE_QUERY_TIMEOUT, float(args.timeout)),
    )
    try:
        # SQLite is opened and remains owned by this child main thread.  The
        # counter is published before the speculative HTTP request completes,
        # allowing a verified lexical snapshot to be sent without waiting for
        # a slow or wedged embedding response.
        online_state = _open_online_verified_generation(
            generation_dir, expected=config
        )
        counter = counter_ready.result()
        reader = GenerationReader(
            generation_dir,
            client,
            counter,
            expected=config,
            _online_state=online_state,
        )
    except BaseException:
        # A speculative vector is never returned or consumed if the sealed
        # generation cannot produce a trusted reader.
        pool.shutdown(wait=True, cancel_futures=True)
        if online_state is not None and online_state[2] is not None:
            online_state[2].close()
        raise
    return reader, pool, precomputed_future


def _child_query_timeout(args: argparse.Namespace) -> float:
    timeout = min(ONLINE_QUERY_TIMEOUT, float(args.timeout))
    deadline = getattr(args, "_query_deadline", None)
    if deadline is not None:
        timeout = min(timeout, float(deadline) - time.monotonic())
    if timeout <= 0:
        raise TimeoutError("online query hard deadline exhausted in worker")
    return timeout


def _query_process(
    args: argparse.Namespace,
    generation: str,
    *,
    lexical_only: bool,
    connection: Any,
) -> None:
    """Run one killable reader and publish a trusted lexical snapshot first."""
    reader: GenerationReader | None = None
    init_pool: ThreadPoolExecutor | None = None
    precomputed_future: Future[PrecomputedQueryEmbedding] | None = None
    child_args = argparse.Namespace(**vars(args))
    child_args.generation = generation
    child_args.timeout = float(getattr(child_args, "timeout", ONLINE_QUERY_TIMEOUT))
    child_args._lexical_only = lexical_only
    try:
        if lexical_only:
            reader = _reader(child_args)
        else:
            reader, init_pool, precomputed_future = _query_reader(child_args)
        if not lexical_only:
            snapshot = reader.search(
                child_args.query,
                top_k=child_args.top_k,
                **({"content_scope":child_args._content_scope} if hasattr(child_args,"_content_scope") else {}),
                use_reranker=False,
                use_dense=False,
                timeout=_child_query_timeout(child_args),
            )
            snapshot["dense_enabled"] = False
            snapshot["reranker_used"] = False
            snapshot["degraded"] = True
            snapshot["degraded_reasons"] = list(
                dict.fromkeys((*snapshot.get("degraded_reasons", []), "lexical_only"))
            )
            connection.send({"ok": True, "phase": "snapshot", "result": snapshot})
            assert precomputed_future is not None and init_pool is not None
            try:
                precomputed = precomputed_future.result()
            finally:
                init_pool.shutdown(wait=True, cancel_futures=True)
                init_pool = None
        else:
            precomputed = None
        result = reader.search(
            child_args.query,
            top_k=child_args.top_k,
            **({"content_scope":child_args._content_scope} if hasattr(child_args,"_content_scope") else {}),
            use_reranker=not child_args.no_rerank and not lexical_only,
            use_dense=not lexical_only,
            timeout=_child_query_timeout(child_args),
            precomputed_query=precomputed,
        )
        connection.send({"ok": True, "phase": "final", "result": result})
    except BaseException as exc:
        trusted_snapshot: dict[str, Any] | None = None
        fallback: GenerationReader | None = None
        if not lexical_only and reader is None:
            try:
                fallback_args = argparse.Namespace(**vars(child_args))
                fallback_args._lexical_only = True
                fallback_args._lexical_degraded_reasons = (
                    f"online_worker_failed:{type(exc).__name__}",
                )
                fallback = _reader(fallback_args)
                trusted_snapshot = fallback.search(
                    fallback_args.query,
                    top_k=fallback_args.top_k,
                    **({"content_scope":fallback_args._content_scope} if hasattr(fallback_args,"_content_scope") else {}),
                    use_reranker=False,
                    use_dense=False,
                    timeout=min(ONLINE_QUERY_TIMEOUT, float(fallback_args.timeout)),
                )
            except BaseException:
                trusted_snapshot = None
            finally:
                if fallback is not None:
                    fallback.close()
        try:
            message = {
                "ok": False,
                "phase": "final",
                "error_type": type(exc).__name__,
                "error": str(exc),
            }
            if trusted_snapshot is not None:
                message["trusted_snapshot"] = trusted_snapshot
            connection.send(message)
        except (BrokenPipeError, EOFError, OSError):
            pass
    finally:
        if init_pool is not None:
            init_pool.shutdown(wait=True, cancel_futures=True)
        if reader is not None:
            reader.close()
        connection.close()


def _stop_query_process(process: multiprocessing.Process) -> None:
    if not process.is_alive():
        process.join(timeout=0)
        process.close()
        return
    process.terminate()
    process.join(timeout=0.03)
    if process.is_alive():
        process.kill()
        process.join(timeout=0.03)
    if not process.is_alive():
        process.close()


def _lexical_only_preflight_reasons(
    args: argparse.Namespace, generation: str
) -> tuple[str, ...]:
    """Detect dense incompatibility without tokenizer, runtime, or vector I/O."""

    index_root = getattr(args, "index_root", None)
    if index_root is None:
        return ()
    generation_dir = Path(index_root).expanduser() / "generations" / generation
    try:
        manifest = load_manifest(generation_dir)
    except ValueError:
        # The killable lexical worker remains the authority for malformed or
        # incomplete manifests.  It will either prove the catalog or reject it
        # without constructing tokenizer/runtime state.
        return ("manifest_preflight_unavailable",)
    reasons: list[str] = []
    if manifest.get("manifest_hash") != compute_manifest_hash(manifest):
        reasons.append("manifest_hash_mismatch")
    if manifest.get("status") not in {"ready", "active", "retired"}:
        reasons.append("generation_incomplete")
    fields = (
        "model_id",
        "model_revision",
        "dimension",
        "dtype",
        "normalized",
        "query_template",
        "document_template",
        "chunker_version",
        "payload_version",
        "reranker_model_id",
        "reranker_revision",
        "hard_max_tokens",
    )
    if any(field not in manifest for field in fields):
        reasons.append("manifest_shape_invalid")
    dimension = manifest.get("dimension")
    row_count = manifest.get("row_count")
    if type(dimension) is not int or type(row_count) is not int or dimension <= 0 or row_count <= 0:
        reasons.append("manifest_shape_invalid")
        expected_size: int | None = None
    else:
        expected_size = dimension * row_count * 4
    vectors = generation_dir / "vectors.f32"
    if vectors.is_symlink() or not vectors.is_file():
        reasons.append("vectors_unavailable")
    else:
        try:
            vector_size = vectors.stat().st_size
        except OSError as exc:
            reasons.append(f"vectors_stat_error:{type(exc).__name__}")
        else:
            if expected_size is not None and vector_size != expected_size:
                reasons.append("vector_blob_length_mismatch")
    if re.fullmatch(r"[0-9a-f]{64}", str(manifest.get("vectors_hash") or "")) is None:
        reasons.append("vectors_hash_invalid")
    return tuple(dict.fromkeys(reasons))


def _native_thread_count() -> int | None:
    """Return the operating system's thread count, failing closed if unknown."""

    if sys.platform.startswith("linux"):
        try:
            return sum(1 for _entry in Path("/proc/self/task").iterdir())
        except OSError:
            return None
    if sys.platform == "darwin":
        try:
            completed = subprocess.run(
                ["/bin/ps", "-M", "-p", str(os.getpid())],
                check=False,
                capture_output=True,
                text=True,
                timeout=1.0,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        lines = [line for line in completed.stdout.splitlines() if line.strip()]
        if completed.returncode != 0 or len(lines) < 2 or "PID" not in lines[0]:
            return None
        return len(lines) - 1
    return None


def _query_process_start_method(requested: str | None = None) -> str | None:
    """Choose fork only when the OS proves the parent has one native thread."""

    available = multiprocessing.get_all_start_methods()
    if requested is not None:
        if requested not in available:
            return None
        if requested == "fork" and _native_thread_count() != 1:
            return None
        return requested
    if "fork" in available and _native_thread_count() == 1:
        return "fork"
    return next(
        (value for value in ("forkserver", "spawn") if value in available),
        None,
    )


def _query_generation_hard_deadline(
    args: argparse.Namespace,
    generation: str,
    *,
    deadline: float,
    start_method: str | None = None,
    _worker_target: Any = None,
) -> dict[str, Any]:
    """Return full hybrid results within the wall clock or a lexical fallback.

    Full and lexical-only readers run in separate processes.  SQLite/NumPy or
    any extension code can therefore be terminated even when it does not
    cooperate with Python deadline checks.
    """
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("online query hard deadline exhausted before start")
    selected_method = _query_process_start_method(start_method)
    if selected_method is None:
        raise RuntimeError("hard-deadline query isolation has no supported process method")
    context = multiprocessing.get_context(selected_method)
    worker_target = _query_process if _worker_target is None else _worker_target
    preflight_reasons = _lexical_only_preflight_reasons(args, generation)
    lexical_preflight = bool(preflight_reasons)
    worker_args = argparse.Namespace(**vars(args))
    worker_args._query_deadline = deadline
    if lexical_preflight:
        existing_reasons = tuple(
            str(value) for value in getattr(args, "_lexical_degraded_reasons", ())
        )
        worker_args._lexical_degraded_reasons = tuple(
            dict.fromkeys((*existing_reasons, *preflight_reasons))
        )
    workers: dict[Any, tuple[multiprocessing.Process, str]] = {}
    worker_specs = ((True, "lexical"),) if lexical_preflight else ((False, "full"),)
    for lexical_only, label in worker_specs:
        parent, child = context.Pipe(duplex=False)
        process = context.Process(
            target=worker_target,
            args=(worker_args, generation),
            kwargs={"lexical_only": lexical_only, "connection": child},
            daemon=True,
            name=f"chatdaily-knowledge-{label}",
        )
        process.start()
        child.close()
        workers[parent] = (process, label)

    lexical: dict[str, Any] | None = None
    full_error: dict[str, Any] | None = None
    lexical_error: dict[str, Any] | None = None
    try:
        # Reserve a small termination margin inside the public hard deadline.
        stop_at = deadline - min(0.10, max(0.01, remaining * 0.05))
        while workers and time.monotonic() < stop_at:
            ready = wait_connections(list(workers), timeout=max(0.0, stop_at - time.monotonic()))
            if not ready:
                break
            for connection in ready:
                process, label = workers[connection]
                try:
                    message = connection.recv()
                except EOFError:
                    message = {
                        "ok": False,
                        "error_type": "WorkerExit",
                        "error": f"{label} query worker exited without a result",
                    }
                if message.get("ok") and message.get("phase") == "snapshot":
                    lexical = dict(message["result"])
                    continue
                workers.pop(connection)
                connection.close()
                _stop_query_process(process)
                if message.get("ok") and label == "full":
                    return dict(message["result"])
                if message.get("ok") and label == "lexical":
                    lexical = dict(message["result"])
                    if lexical_preflight:
                        return lexical
                elif label == "full":
                    full_error = message
                    if message.get("trusted_snapshot") is not None:
                        lexical = dict(message["trusted_snapshot"])
                elif label == "lexical":
                    lexical_error = message
                if full_error is not None and lexical is not None:
                    break
            if full_error is not None and lexical is not None:
                break

        if lexical is not None:
            reasons = list(lexical.get("degraded_reasons") or [])
            if full_error is None:
                reasons.append("online_hard_deadline_exceeded")
            else:
                reasons.append(f"online_worker_failed:{full_error.get('error_type', 'Unknown')}")
            lexical["degraded"] = True
            lexical["reranker_used"] = False
            lexical["degraded_reasons"] = list(dict.fromkeys(reasons))
            timings = dict(lexical.get("timing_ms") or {})
            timings["hard_deadline_budget"] = round(
                min(
                    ONLINE_QUERY_TIMEOUT,
                    float(getattr(args, "timeout", ONLINE_QUERY_TIMEOUT)),
                )
                * 1000.0,
                2,
            )
            lexical["timing_ms"] = timings
            return lexical
        if full_error is not None:
            raise ValueError(
                f"query worker failed: {full_error.get('error_type')}: {full_error.get('error')}"
            )
        if lexical_error is not None:
            raise ValueError(
                "lexical query worker failed: "
                f"{lexical_error.get('error_type')}: {lexical_error.get('error')}"
            )
        raise TimeoutError("online query exceeded the absolute wall-clock deadline")
    finally:
        for connection, (process, _label) in list(workers.items()):
            connection.close()
            _stop_query_process(process)


def cmd_query(args: argparse.Namespace) -> int:
    release = _require_knowledge_retrieval(args)
    current = _current_pointer_for_query(args.index_root)
    if release.get("candidate_generation") != current:
        raise ValueError("open knowledge release CURRENT changed before query execution")
    if args.generation and args.generation != current:
        raise ValueError(
            "regular production query generation must match CURRENT; "
            "use diagnostic-query for an explicit non-CURRENT generation"
        )
    generation = current
    generation_context: dict[str, Any] | None
    lexical_reason: str | None
    fallback_reason: str | None = None
    try:
        generation_context, lexical_reason = _bind_release_generation(
            args.index_root,
            current,
            str(release["candidate_artifact_sha256"]),
        )
    except (OSError, ValueError) as candidate_error:
        baseline = str(release["baseline_generation"])
        if read_pointer(args.index_root, "PREVIOUS") != baseline:
            raise ValueError("open knowledge release PREVIOUS changed before fallback") from candidate_error
        generation_context, lexical_reason = _bind_release_generation(
            args.index_root,
            baseline,
            str(release["baseline_artifact_sha256"]),
        )
        generation = baseline
        fallback_reason = f"{type(candidate_error).__name__}: {candidate_error}"
    args._lexical_degraded_reasons = tuple(
        value for value in (lexical_reason, fallback_reason) if value
    )
    timeout = float(getattr(args, "timeout", ONLINE_QUERY_TIMEOUT))
    deadline = time.monotonic() + min(ONLINE_QUERY_TIMEOUT, timeout)
    started = time.perf_counter()
    try:
        result = _query_generation_hard_deadline(
            args,
            generation,
            deadline=deadline,
        )
    except (OSError, RuntimeError, TimeoutError, ValueError, sqlite3.DatabaseError) as exc:
        if generation != current:
            raise
        baseline = str(release["baseline_generation"])
        baseline_context, baseline_lexical_reason = _bind_release_generation(
            args.index_root,
            baseline,
            str(release["baseline_artifact_sha256"]),
        )
        generation = baseline
        generation_context = baseline_context
        fallback_reason = f"{type(exc).__name__}: {exc}"
        args._lexical_degraded_reasons = tuple(
            value for value in (baseline_lexical_reason, fallback_reason) if value
        )
        result = _query_generation_hard_deadline(args, generation, deadline=deadline)
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    result["served_generation"] = generation
    if fallback_reason is not None:
        result["fallback_reason"] = fallback_reason
        reasons = list(result.get("degraded_reasons") or [])
        reasons.append(f"generation_fallback:{fallback_reason.split(':', 1)[0]}")
        result["degraded_reasons"] = list(dict.fromkeys(reasons))
        result["degraded"] = True
    telemetry_recorded = False
    telemetry_error: str | None = None
    if (
        release.get("status") == RELEASE_OPEN
        and generation == current
        and generation_context is not None
    ):
        reranker_error = any(
            str(reason).startswith("rerank_failed:")
            for reason in result.get("degraded_reasons", [])
        )
        request_id = str(getattr(args, "request_id", "") or os.urandom(32).hex())
        try:
            append_shadow_event(
                _shadow_journal(args),
                {
                    "kind": "query",
                    "generation_id": current,
                    "route": "candidate",
                    "latency_ms": elapsed_ms,
                    "selected_candidate": True,
                    "served_route": "candidate",
                    "candidate_latency_ms": elapsed_ms,
                    "total_latency_ms": elapsed_ms,
                    "reranker_attempted": bool(result.get("reranker_used"))
                    or reranker_error,
                    "reranker_error": reranker_error,
                    "request_hash": hashlib.sha256(request_id.encode("utf-8")).hexdigest(),
                    "fallback_reason": fallback_reason,
                    "generation_context": generation_context,
                },
            )
            telemetry_recorded = True
        except (OSError, ValueError) as exc:
            # Retrieval is side-band and remains fail-open.  Missing telemetry
            # makes the guard window invalid rather than inventing healthy
            # evidence, while the successful query result is still served.
            telemetry_error = type(exc).__name__
    elif generation_context is None:
        telemetry_error = "GenerationContextInvalid"
    elif generation != current:
        telemetry_error = "BaselineFallback"
    result["telemetry_recorded"] = telemetry_recorded
    if telemetry_error is not None:
        result["telemetry_error"] = telemetry_error
    _json(result)
    return 0


def cmd_task(args: argparse.Namespace) -> int:
    """Use the existing query handlers and retain their authorization and fallback."""
    import contextlib
    import io
    from chat_daily_tg.content_feedback import read_rows
    from chat_daily_tg.knowledge_tasks import task_results, read_scope
    feedback=read_rows(args.feedback)
    delivered={r.get("content_id") for r in read_rows(args.delivered_ledger)
               if r.get("delivery_state")=="confirmed" and r.get("content_id")}
    read_ids,read_count,_unresolved=read_scope(feedback)
    now=datetime.now(timezone.utc)
    args._content_scope=({'published_after':(now-timedelta(days=7)).isoformat(),'published_before':now.isoformat()}
                         if args.task=='progress' else {'content_ids':sorted(read_ids if read_count else delivered)})
    started=time.monotonic()
    original_timeout=float(args.timeout)
    def run_scoped():
        buffer=io.StringIO()
        with contextlib.redirect_stdout(buffer):
            if args.diagnostic:
                if not args.generation:raise ValueError("diagnostic task requires explicit generation")
                cmd_diagnostic_query(args)
            else:cmd_query(args)
        payload=json.loads(buffer.getvalue())
        result=payload.get("result",payload)
        result["diagnostic"]=bool(args.diagnostic)
        result.setdefault("generation_id",payload.get("generation_id"))
        result["scope_filter"]="applied before exact/FTS/dense candidate limits"
        return result
    result=run_scoped()
    first=task_results(result,task=args.task,feedback=feedback,delivered_ids=delivered,now=now)
    if not first['results'] and args.expand_archive and args.task=='recall':
        remaining=min(ONLINE_QUERY_TIMEOUT,original_timeout)-(time.monotonic()-started)
        if remaining>0:
            args._content_scope=None
            args.timeout=remaining
            if getattr(args,'request_id',None):args.request_id += ':expanded'
            result=run_scoped()
        else:
            result['expansion_error']='query_deadline_exhausted'
    events = []
    if args.event_root:
        for path in args.event_root.glob("*.event.json"):
            try:
                event = json.loads(path.read_text(encoding="utf-8"))
                dossier = path.with_name(path.name.removesuffix(".event.json") + ".md")
                if dossier.is_file():
                    events.append({**event, "path": str(dossier)})
            except (OSError, ValueError, TypeError):
                continue
    result["task_elapsed_ms"]=round((time.monotonic()-started)*1000,2)
    _json(task_results(result, task=args.task, feedback=feedback, now=now,
                       delivered_ids=delivered, expand_archive=args.expand_archive,
                       event_archives=events))
    return 0


def cmd_bootstrap(args: argparse.Namespace) -> int:
    """Seed a verified CURRENT baseline without creating release authority."""

    if _read_index_release_state(args.index_root) is not None:
        raise ValueError("bootstrap requires knowledge release state to be absent")
    expected, _runtime, _model_path = _runtime_generation_config(args)
    result = bootstrap_generation(
        args.index_root,
        args.generation,
        expected=expected,
    )
    # Re-read after the pointer mutation so a concurrent release writer cannot
    # turn a bootstrap command into an implicit production authorization.
    if _read_index_release_state(args.index_root) is not None:
        raise ValueError("knowledge release state appeared during bootstrap")
    result["release_state"] = None
    result["telemetry_recorded"] = False
    _json(result)
    return 0


def cmd_diagnostic_query(args: argparse.Namespace) -> int:
    """Query one explicit generation without production release authorization."""

    timeout = float(getattr(args, "timeout", ONLINE_QUERY_TIMEOUT))
    deadline = time.monotonic() + min(ONLINE_QUERY_TIMEOUT, timeout)
    result = _query_generation_hard_deadline(
        args,
        args.generation,
        deadline=deadline,
    )
    _json(
        {
            "schema": "chatdaily-knowledge-diagnostic-query.v1",
            "diagnostic": True,
            "production_authorized": False,
            "generation_id": args.generation,
            "telemetry_recorded": False,
            "result": result,
        }
    )
    return 0


def _shadow_journal(args: argparse.Namespace) -> Path:
    return args.journal or args.index_root / "shadow" / "events.jsonl"


def _generation_shadow_context(
    index_root: Path, generation_id: str
) -> dict[str, Any]:
    generation_dir = (
        Path(index_root).expanduser() / "generations" / generation_id
    )
    manifest_path = generation_dir / "manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ValueError("shadow generation manifest must be a regular non-symlink file")
    before = sha256_file(manifest_path)
    manifest = load_manifest(generation_dir)
    if sha256_file(manifest_path) != before:
        raise ValueError("shadow generation manifest changed while binding context")
    if manifest.get("manifest_hash") != compute_manifest_hash(manifest):
        raise ValueError("shadow generation manifest identity is invalid")
    immutable = {
        "generation_id": generation_id,
        "manifest_hash": manifest.get("manifest_hash"),
        "catalog_hash": manifest.get("catalog_hash"),
        "vectors_hash": manifest.get("vectors_hash"),
    }
    for field in ("manifest_hash", "catalog_hash", "vectors_hash"):
        if re.fullmatch(r"[0-9a-f]{64}", str(immutable.get(field) or "")) is None:
            raise ValueError(f"shadow generation {field} is invalid")
    artifact_sha256 = hashlib.sha256(
        json.dumps(
            immutable,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    context = {
        "schema": GENERATION_CONTEXT_SCHEMA,
        "generation_id": generation_id,
        "artifact_sha256": artifact_sha256,
        "manifest_hash": immutable["manifest_hash"],
        "catalog_hash": immutable["catalog_hash"],
        "vectors_hash": immutable["vectors_hash"],
        "model_id": manifest.get("model_id"),
        "model_revision": manifest.get("model_revision"),
        "reranker_model_id": manifest.get("reranker_model_id"),
        "reranker_revision": manifest.get("reranker_revision"),
        "dimension": manifest.get("dimension"),
    }
    # Reuse the journal validator as the one strict public context contract.
    return validate_generation_context(context, generation_id=generation_id)


def _declared_lexical_artifact_binding(
    index_root: Path, generation_id: str
) -> dict[str, Any]:
    """Bind a parseable degraded manifest to its unchanged catalog artifact.

    The release state seals the manifest/catalog/vector hash tuple.  When the
    manifest's dense reader context no longer validates, that tuple can still
    authorize an exact/FTS-only attempt provided the actual catalog matches the
    declared catalog hash.  This helper never authorizes dense or rerank use.
    """

    generation_dir = Path(index_root).expanduser() / "generations" / generation_id
    manifest_path = generation_dir / "manifest.json"
    catalog_path = generation_dir / "catalog.sqlite"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ValueError("degraded generation manifest must be a regular non-symlink file")
    if catalog_path.is_symlink() or not catalog_path.is_file():
        raise ValueError("degraded generation catalog must be a regular non-symlink file")
    before = sha256_file(manifest_path)
    manifest = load_manifest(generation_dir)
    immutable = {
        "generation_id": generation_id,
        "manifest_hash": manifest.get("manifest_hash"),
        "catalog_hash": manifest.get("catalog_hash"),
        "vectors_hash": manifest.get("vectors_hash"),
    }
    for field in ("manifest_hash", "catalog_hash", "vectors_hash"):
        if re.fullmatch(r"[0-9a-f]{64}", str(immutable.get(field) or "")) is None:
            raise ValueError(f"degraded generation {field} is invalid")
    if sha256_file(manifest_path) != before:
        raise ValueError("degraded generation manifest changed while binding")
    if sha256_file(catalog_path) != immutable["catalog_hash"]:
        raise ValueError("degraded generation catalog seal mismatch")
    artifact_sha256 = hashlib.sha256(
        json.dumps(immutable, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "generation_id": generation_id,
        "artifact_sha256": artifact_sha256,
        "manifest_sha256": before,
        "manifest_identity_valid": manifest.get("manifest_hash")
        == compute_manifest_hash(manifest),
        "catalog_hash": immutable["catalog_hash"],
    }


def _bind_release_generation(
    index_root: Path,
    generation_id: str,
    expected_artifact_sha256: str,
) -> tuple[dict[str, Any] | None, str | None]:
    """Return strict context or one release-bound lexical degradation reason."""

    try:
        context = _generation_shadow_context(index_root, generation_id)
    except (OSError, ValueError) as strict_error:
        declared = _declared_lexical_artifact_binding(index_root, generation_id)
        if declared["artifact_sha256"] != expected_artifact_sha256:
            raise ValueError("degraded generation is not bound to release state") from strict_error
        return None, f"generation_context_invalid:{type(strict_error).__name__}"
    if context["artifact_sha256"] != expected_artifact_sha256:
        raise ValueError("generation artifact identity changed after release")
    return context, None


def cmd_canary_query(args: argparse.Namespace) -> int:
    """Route a stable 10% cohort to a candidate without changing fact state."""

    release = _require_knowledge_retrieval(args, canary=True)
    if release is None:  # Defensive: authorization above already proved it.
        raise ValueError("knowledge canary release state disappeared")

    selected = canary_selected(
        args.request_id,
        args.candidate,
        percent=args.percent,
    )
    current = _current_pointer_for_query(args.index_root)
    if current != args.candidate:
        raise ValueError("canary CURRENT changed after release authorization")
    baseline = str(release["baseline_generation"])
    if baseline == args.candidate:
        raise ValueError("canary candidate and baseline must be different generations")
    candidate_context: dict[str, Any] | None = None
    candidate_binding_error: str | None = None
    candidate_lexical_reason: str | None = None
    try:
        candidate_context, candidate_lexical_reason = _bind_release_generation(
            args.index_root,
            args.candidate,
            str(release["candidate_artifact_sha256"]),
        )
    except (OSError, ValueError) as exc:
        candidate_binding_error = f"{type(exc).__name__}: {exc}"
    _baseline_context, baseline_lexical_reason = _bind_release_generation(
        args.index_root,
        baseline,
        str(release["baseline_artifact_sha256"]),
    )
    target = baseline
    fallback_reason: str | None = None
    started = time.perf_counter()
    timeout = float(getattr(args, "timeout", ONLINE_QUERY_TIMEOUT))
    overall_budget = min(ONLINE_QUERY_TIMEOUT, timeout)
    deadline = time.monotonic() + overall_budget
    candidate_latency_ms = 0.0
    candidate_result: dict[str, Any] | None = None

    def run(
        generation_id: str,
        query_deadline: float,
        *degraded_reasons: str | None,
    ) -> dict[str, Any]:
        child_args = argparse.Namespace(**vars(args))
        child_args._lexical_degraded_reasons = tuple(
            value for value in degraded_reasons if value
        )
        return _query_generation_hard_deadline(
            child_args,
            generation_id,
            deadline=query_deadline,
        )

    if selected and candidate_context is not None and candidate_lexical_reason is None:
        # Reserve enough of the same public eight-second budget for one and
        # only one baseline fallback.  The fallback still receives the overall
        # absolute deadline, so candidate+baseline can never become 8s+8s.
        reserve = min(2.0, max(0.0, overall_budget / 2.0))
        candidate_deadline = deadline - reserve
        candidate_started = time.perf_counter()
        try:
            candidate_result = run(args.candidate, candidate_deadline)
            result = candidate_result
            target = args.candidate
        except (
            OSError,
            RuntimeError,
            TimeoutError,
            ValueError,
            sqlite3.DatabaseError,
        ) as exc:
            fallback_reason = f"{type(exc).__name__}: {exc}"
            result = run(baseline, deadline, baseline_lexical_reason, fallback_reason)
        finally:
            candidate_latency_ms = (time.perf_counter() - candidate_started) * 1000.0
    else:
        if selected:
            fallback_reason = candidate_binding_error or candidate_lexical_reason or (
                "candidate_generation_context_invalid"
            )
        result = run(baseline, deadline, baseline_lexical_reason, fallback_reason)

    elapsed_ms = (time.perf_counter() - started) * 1000.0
    route = "candidate" if selected and target == args.candidate else "baseline"
    recorded = False
    record_error: str | None = None
    if args.record and candidate_context is not None:
        telemetry_result = candidate_result or {}
        reranker_error = any(
            str(reason).startswith("rerank_failed:")
            for reason in telemetry_result.get("degraded_reasons", [])
        )
        append_shadow_event(
            _shadow_journal(args),
            {
                "kind": "query",
                "generation_id": args.candidate,
                "route": route,
                "latency_ms": elapsed_ms,
                "selected_candidate": selected,
                "served_route": route,
                "candidate_latency_ms": candidate_latency_ms,
                "total_latency_ms": elapsed_ms,
                "reranker_attempted": bool(telemetry_result.get("reranker_used"))
                or reranker_error,
                "reranker_error": reranker_error,
                "request_hash": hashlib.sha256(args.request_id.encode("utf-8")).hexdigest(),
                "fallback_reason": fallback_reason,
                "generation_context": candidate_context,
            },
        )
        recorded = True
    elif args.record:
        record_error = "candidate_generation_context_invalid"
    _json(
        {
            "schema": "chatdaily-knowledge-canary-query.v1",
            "candidate_generation": args.candidate,
            "baseline_generation": baseline,
            "selected": selected,
            "percent": args.percent,
            "served_generation": target,
            "route": route,
            "fallback_reason": fallback_reason,
            "recorded": recorded,
            "record_error": record_error,
            "candidate_latency_ms": round(candidate_latency_ms, 2),
            "elapsed_ms": round(elapsed_ms, 2),
            "result": result,
        }
    )
    return 0


def cmd_shadow_record(args: argparse.Namespace) -> int:
    raw = json.loads(args.event.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("shadow event file must contain one JSON object")
    if "timestamp" in raw:
        raise ValueError("shadow-record timestamps are assigned live and cannot be backfilled")
    if raw.get("kind") in {
        "health",
        "query",
        "incremental",
        "incremental_refresh_receipt",
        "source_freshness",
        "source_link",
    }:
        raise ValueError(
            "release shadow evidence must be emitted by its authoritative command"
        )
    raw["timestamp"] = shadow_utc_now()
    recorded = append_shadow_event(_shadow_journal(args), raw)
    _json({"status": "recorded", "journal": str(_shadow_journal(args)), "event": recorded})
    return 0


def cmd_shadow_audit_sources(args: argparse.Namespace) -> int:
    """Append source/cursor evidence derived from one authoritative snapshot."""

    snapshot = collect_sources(_source_paths(args))
    generation_dir = (
        Path(args.index_root).expanduser() / "generations" / args.generation
    )
    incremental = audit_incremental_freshness(generation_dir, snapshot)
    source_links = audit_source_links(generation_dir, snapshot)
    generation_context = _generation_shadow_context(
        args.index_root, args.generation
    )
    incremental["generation_context"] = generation_context
    source_links["generation_context"] = generation_context
    recorded = [
        append_shadow_event(_shadow_journal(args), incremental),
        append_shadow_event(_shadow_journal(args), source_links),
    ]
    passed = incremental.get("success") is True and source_links.get("accurate") is True
    _json(
        {
            "status": "ok" if passed else "failed",
            "generation_id": args.generation,
            "read_only": True,
            "events": recorded,
        }
    )
    return 0 if passed else 2


def cmd_incremental_refresh(args: argparse.Namespace) -> int:
    """Rebuild current facts into a fresh sealed generation and receipt."""

    candidate = str(args.shadow_candidate)
    output_generation = str(args.output_generation)
    generation_pattern = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
    for field, value in (
        ("shadow candidate", candidate),
        ("output generation", output_generation),
    ):
        if value in {"", ".", ".."} or generation_pattern.fullmatch(value) is None:
            raise ValueError(f"incremental refresh {field} is invalid")
    if candidate == output_generation:
        raise ValueError("incremental refresh output must differ from shadow candidate")

    index_root = Path(args.index_root).expanduser()
    candidate_dir = index_root / "generations" / candidate
    output_dir = index_root / "generations" / output_generation
    if output_dir.exists() or output_dir.is_symlink():
        raise FileExistsError(f"incremental refresh output already exists: {output_dir}")

    config, counter, client = _runtime_objects(args)
    candidate_artifact_before = generation_artifact_identity(
        candidate_dir,
        expected_generation_id=candidate,
    )
    candidate_verification = verify_generation(
        candidate_dir,
        expected=config,
        full=True,
    )
    if not candidate_verification["ok"]:
        raise ValueError(
            "incremental refresh candidate failed full verification: "
            + ",".join(candidate_verification["errors"])
        )
    candidate_context = _generation_shadow_context(index_root, candidate)

    snapshot = collect_sources(_source_paths(args))
    freshness = audit_incremental_freshness(candidate_dir, snapshot)
    freshness.update(
        {
            "producer": INCREMENTAL_REFRESH_PRODUCER,
            "generation_context": candidate_context,
            "refresh_performed": False,
        }
    )
    recorded_freshness = append_shadow_event(_shadow_journal(args), freshness)
    if freshness.get("noop") is True and freshness.get("success") is True:
        _json(
            {
                "status": "noop",
                "generation_id": candidate,
                "output_generation_id": None,
                "refresh_performed": False,
                "source_freshness": recorded_freshness,
            }
        )
        return 0
    if freshness.get("success") is not True:
        _json(
            {
                "status": "failed",
                "generation_id": candidate,
                "output_generation_id": None,
                "refresh_performed": False,
                "source_freshness": recorded_freshness,
            }
        )
        return 2

    builder = GenerationBuilder(index_root, config, counter, client)
    build_report = builder.build(
        snapshot.documents,
        source_cursors=snapshot.cursors,
        feedback_events=snapshot.feedback_events,
        generation_id=output_generation,
        baseline_generation_id=candidate,
        baseline_pointer="INCREMENTAL",
    )
    output_verification = verify_generation(output_dir, expected=config, full=True)
    if not output_verification["ok"]:
        raise ValueError(
            "incremental refresh output failed full verification: "
            + ",".join(output_verification["errors"])
        )
    output_freshness = audit_incremental_freshness(output_dir, snapshot)
    if output_freshness.get("success") is not True or output_freshness.get("noop") is not True:
        raise ValueError("incremental refresh output source cursors do not match snapshot")
    output_links = audit_source_links(output_dir, snapshot)
    if output_links.get("accurate") is not True:
        raise ValueError("incremental refresh output source links do not match snapshot")

    candidate_context_after = _generation_shadow_context(index_root, candidate)
    candidate_artifact_after = generation_artifact_identity(
        candidate_dir,
        expected_generation_id=candidate,
    )
    if (
        candidate_context_after != candidate_context
        or candidate_artifact_after != candidate_artifact_before
    ):
        raise ValueError("incremental refresh candidate changed during rebuild")
    output_context = _generation_shadow_context(index_root, output_generation)
    receipt = {
        "kind": "incremental_refresh_receipt",
        "producer": INCREMENTAL_REFRESH_PRODUCER,
        "generation_id": candidate,
        "generation_context": candidate_context,
        "success": True,
        "refresh_performed": True,
        "noop": False,
        "baseline_generation_id": candidate,
        "baseline_generation_context": candidate_context,
        "output_generation_id": output_generation,
        "output_generation_context": output_context,
        "baseline_source_cursor_hash": freshness["actual_hash"],
        "source_snapshot_hash": freshness["expected_hash"],
        "output_source_cursor_hash": output_freshness["actual_hash"],
        "error": None,
    }
    recorded_receipt = append_shadow_event(_shadow_journal(args), receipt)
    _json(
        {
            "status": "refreshed",
            "generation_id": candidate,
            "output_generation_id": output_generation,
            "refresh_performed": True,
            "source_freshness": recorded_freshness,
            "receipt": recorded_receipt,
            "build": build_report,
            "verification": output_verification,
            "source_links": output_links,
        }
    )
    return 0


def cmd_shadow_status(args: argparse.Namespace) -> int:
    generation_context = _generation_shadow_context(
        args.index_root, args.generation
    )
    report = summarize_shadow(
        _shadow_journal(args),
        args.generation,
        window_days=args.window_days,
        generation_context=generation_context,
    )
    _json(report)
    return 0 if report["ready"] else 2


def cmd_shadow_probe(args: argparse.Namespace) -> int:
    runtime = _runtime_config(args.runtime_config)
    token_env = str(runtime.get("token_env") or "QWEN_VL_RUNTIME_TOKEN")
    token = os.environ.get(token_env, "")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    base = args.endpoint or (
        f"http://{runtime.get('bind', '127.0.0.1')}:{runtime.get('port', 8790)}/v1"
    )
    url = base.rstrip("/") + "/health"
    generation_context = _generation_shadow_context(
        args.index_root, args.generation
    )
    observation: dict[str, Any] = {
        "kind": "health",
        "generation_id": args.generation,
        "available": False,
        "generation_context": generation_context,
    }
    try:
        response = httpx.get(
            url,
            headers=headers,
            timeout=min(float(args.timeout), 8.0),
            trust_env=False,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("health response must be an object")
        embedding = payload.get("embedding")
        reranker = payload.get("reranker")
        health_embedding_model = payload.get("embedding_model")
        health_reranker_model = payload.get("reranker_model")
        embedding_revision = payload.get("embedding_revision")
        reranker_revision = payload.get("reranker_revision")
        if isinstance(embedding, dict):
            health_embedding_model = health_embedding_model or embedding.get("model")
            embedding_revision = embedding_revision or embedding.get("revision")
        if isinstance(reranker, dict):
            health_reranker_model = health_reranker_model or reranker.get("model")
            reranker_revision = reranker_revision or reranker.get("revision")
        config_identity_matches = (
            str(runtime.get("embedding_model") or EMBEDDING_MODEL)
            == generation_context["model_id"]
            and str(runtime.get("reranker_model") or RERANKER_MODEL)
            == generation_context["reranker_model_id"]
        )
        health_revision_matches = (
            embedding_revision == generation_context["model_revision"]
            and reranker_revision == generation_context["reranker_revision"]
        )
        # The deployed health contract always attests revisions.  Model IDs
        # are authoritative in the local runtime config and in request/response
        # payloads; when a runtime also exposes them in health, require an exact
        # match rather than weakening the check.
        health_model_matches = (
            health_embedding_model in {None, generation_context["model_id"]}
            and health_reranker_model
            in {None, generation_context["reranker_model_id"]}
        )
        identity_matches = (
            config_identity_matches
            and health_revision_matches
            and health_model_matches
        )
        observation["available"] = bool(
            payload.get("ready") is True
            and isinstance(embedding, dict)
            and embedding.get("ready") is True
            and isinstance(reranker, dict)
            and reranker.get("ready") is True
            and identity_matches
        )
        observation["http_status"] = response.status_code
        observation["embedding_ready"] = bool(
            isinstance(payload.get("embedding"), dict) and payload["embedding"].get("ready") is True
        )
        observation["reranker_ready"] = bool(
            isinstance(payload.get("reranker"), dict) and payload["reranker"].get("ready") is True
        )
        observation["runtime_identity_matches"] = identity_matches
        observation["runtime_config_identity_matches"] = config_identity_matches
        observation["health_revision_matches"] = health_revision_matches
        if not identity_matches:
            observation["error"] = "runtime_identity_mismatch"
    except (httpx.HTTPError, ValueError, json.JSONDecodeError) as exc:
        observation["error"] = type(exc).__name__
    recorded = append_shadow_event(_shadow_journal(args), observation)
    _json({"status": "available" if recorded["available"] else "unavailable", "event": recorded})
    return 0 if recorded["available"] else 2


def cmd_evaluate(args: argparse.Namespace) -> int:
    reader = _reader(args)
    baseline_reader: GenerationReader | None = None
    try:
        _require_complete_evaluation_reader(reader, "candidate")
        baseline_generation = getattr(args, "baseline_generation", None)
        if baseline_generation:
            baseline_args = argparse.Namespace(**vars(args))
            baseline_args.generation = baseline_generation
            baseline_runtime_config = getattr(args, "baseline_runtime_config", None)
            if baseline_runtime_config is not None:
                baseline_args.runtime_config = baseline_runtime_config
            baseline_endpoint = getattr(args, "baseline_endpoint", None)
            if baseline_endpoint is not None:
                baseline_args.endpoint = baseline_endpoint
            baseline_reader = _reader(baseline_args)
            _require_complete_evaluation_reader(baseline_reader, "baseline")
            report = evaluate_release_pair(
                reader,
                baseline_reader,
                args.gold,
                allow_small=args.allow_small,
            )
        else:
            report = evaluate_gold_set(
                reader,
                args.gold,
                allow_small=args.allow_small,
                baseline_e2e_p95_ms=args.baseline_e2e_p95_ms,
            )
    finally:
        if baseline_reader is not None:
            baseline_reader.close()
        reader.close()
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    _json(report)
    return 0 if report["passed"] else 2


def _require_complete_evaluation_reader(reader: GenerationReader, label: str) -> None:
    """Never let a lexical/degraded reader become formal paired evidence."""

    verification = getattr(reader, "verification", None)
    if isinstance(verification, dict) and verification.get("ok") is not True:
        raise ValueError(f"paired evaluation {label} generation is not fully verified")
    if getattr(reader, "dense_enabled", True) is not True:
        reasons = ",".join(str(value) for value in getattr(reader, "degraded_reasons", []))
        raise ValueError(
            f"paired evaluation {label} runtime context does not match its manifest: {reasons}"
        )
    degraded = list(getattr(reader, "degraded_reasons", []) or [])
    if degraded:
        raise ValueError(
            f"paired evaluation {label} reader is degraded: "
            + ",".join(str(value) for value in degraded)
        )
    client = getattr(reader, "client", None)
    if isinstance(client, QwenRuntimeClient):
        try:
            client.health()
        except (OSError, httpx.HTTPError, QwenRuntimeError) as exc:
            raise ValueError(
                f"paired evaluation {label} runtime failed revision attestation"
            ) from exc


def _stable_release_input_hash(path: Path, label: str) -> tuple[Path, str]:
    candidate = Path(path).expanduser().absolute()
    if candidate.is_symlink() or not candidate.is_file():
        raise ValueError(f"{label} must be a regular non-symlink file")
    return candidate, sha256_file(candidate)


def cmd_activate(args: argparse.Namespace) -> int:
    if not args.evaluation:
        raise ValueError("activation requires --evaluation from the frozen >=200-query gold set")
    evaluation_path, evaluation_hash = _stable_release_input_hash(
        args.evaluation, "activation evaluation"
    )
    shadow_path, shadow_hash = _stable_release_input_hash(
        args.shadow_journal, "activation shadow journal"
    )
    candidate_context = _generation_shadow_context(
        args.index_root, args.generation
    )
    shadow = summarize_shadow(
        shadow_path,
        args.generation,
        window_days=7,
        generation_context=candidate_context,
    )
    if not shadow["ready"]:
        raise ValueError(
            "activation requires a complete seven-day shadow: "
            + ",".join(shadow["failure_reasons"])
        )
    if sha256_file(shadow_path) != shadow_hash:
        raise ValueError("activation shadow journal changed during release validation")
    current = resolve_current_generation(args.index_root)
    baseline = (
        read_pointer(args.index_root, "PREVIOUS")
        if current == args.generation
        else current
    )
    generations_root = Path(args.index_root).expanduser() / "generations"
    candidate_dir = generations_root / args.generation
    baseline_dir = generations_root / baseline
    evaluation = validate_evaluation_for_activation(
        evaluation_path,
        args.generation,
        candidate_generation_dir=candidate_dir,
        baseline_generation_dir=baseline_dir,
    )
    baseline_context = _generation_shadow_context(args.index_root, baseline)
    if sha256_file(evaluation_path) != evaluation_hash:
        raise ValueError("activation evaluation changed during release validation")
    expected, _, _ = _runtime_objects(args)
    evaluation_baseline = evaluation.get("baseline_generation_id")
    if evaluation_baseline is not None and evaluation_baseline != baseline:
        raise ValueError("activation evaluation baseline does not match switching CURRENT")
    release = prepare_release(
        _release_state_path(args.index_root),
        candidate_generation=args.generation,
        baseline_generation=baseline,
        candidate_artifact_sha256=candidate_context["artifact_sha256"],
        baseline_artifact_sha256=baseline_context["artifact_sha256"],
        evaluation_hash=evaluation_hash,
        shadow_journal_hash=shadow_hash,
    )
    if sha256_file(evaluation_path) != evaluation_hash:
        raise ValueError("activation evaluation changed after release preparation")
    if sha256_file(shadow_path) != shadow_hash:
        raise ValueError("activation shadow journal changed after release preparation")
    final_current = resolve_current_generation(args.index_root)
    if final_current != args.generation and final_current != baseline:
        raise ValueError("activation baseline changed before pointer mutation")
    if final_current == args.generation:
        if read_pointer(args.index_root, "PREVIOUS") != baseline:
            raise ValueError("activation receipt baseline does not match PREVIOUS")
    # The durable canary_pending record is intentionally published before the
    # pointer switch.  A failed switch leaves canary authorization closed
    # because candidate != CURRENT; a retry reuses the same pending record.
    result = activate_generation(
        args.index_root,
        args.generation,
        expected=expected,
        baseline_generation_id=baseline,
    )
    active_context = _generation_shadow_context(args.index_root, args.generation)
    if active_context["artifact_sha256"] != candidate_context["artifact_sha256"]:
        raise ValueError("candidate artifact changed during activation")
    result["shadow"] = shadow
    result["release_state"] = release
    _json(result)
    return 0


def _release_incident_candidates(index_root: Path) -> list[Path]:
    root = Path(index_root).expanduser()
    paths: list[Path] = []
    legacy = root / ROLLBACK_INCIDENT
    if legacy.exists() or legacy.is_symlink():
        paths.append(legacy)
    incident_dir = root / ROLLBACK_INCIDENT_DIR
    if incident_dir.exists() or incident_dir.is_symlink():
        if incident_dir.is_symlink() or not incident_dir.is_dir():
            raise ValueError("rollback incident directory is unsafe")
        paths.extend(sorted(incident_dir.glob("*.json")))
    return paths


def _clear_proven_release_kill_switch(
    index_root: Path, release: dict[str, Any]
) -> dict[str, Any]:
    """Clear only a kill switch provably owned by the release baseline's incident."""

    root = Path(index_root).expanduser()
    kill_path = root / RETRIEVAL_KILL_SWITCH
    if not kill_path.exists() and not kill_path.is_symlink():
        return {"status": "absent"}
    if kill_path.is_symlink() or not kill_path.is_file():
        return {"status": "manual_required", "reason": "unsafe_kill_switch_path"}
    try:
        kill_hash = sha256_file(kill_path)
        kill = _read_regular_json(kill_path)
        if (
            kill is None
            or kill.get("schema") != "chatdaily-knowledge-retrieval-kill.v1"
            or kill.get("disabled") is not True
            or re.fullmatch(r"[0-9a-f]{64}", str(kill.get("incident_id") or "")) is None
        ):
            raise ValueError("kill switch schema or incident binding is invalid")
        matches: list[tuple[Path, dict[str, Any], str]] = []
        for path in _release_incident_candidates(root):
            if path.is_symlink() or not path.is_file():
                raise ValueError("rollback incident path is unsafe")
            digest = sha256_file(path)
            incident = _read_regular_json(path)
            if incident and incident.get("incident_id") == kill["incident_id"]:
                matches.append((path, incident, digest))
        if len(matches) != 1:
            raise ValueError("kill switch incident is not uniquely traceable")
        incident_path, incident, incident_hash = matches[0]
        if (
            incident.get("schema") != "chatdaily-knowledge-rollback-incident.v1"
            or incident.get("status") != "complete"
            or incident.get("safe_generation") != release["baseline_generation"]
            or incident.get("failed_generation")
            in {release["candidate_generation"], release["baseline_generation"]}
        ):
            raise ValueError("kill switch incident does not describe the release baseline")
        prepared_at = _parse_guard_time(release["prepared_at"], "release prepared_at")
        completed_at = _parse_guard_time(incident.get("completed_at"), "incident completed_at")
        recorded_at = _parse_guard_time(kill.get("recorded_at"), "kill recorded_at")
        if completed_at > prepared_at or recorded_at > prepared_at:
            raise ValueError("kill switch incident is newer than the pending release")
        if sha256_file(kill_path) != kill_hash or sha256_file(incident_path) != incident_hash:
            raise ValueError("kill switch evidence changed during promotion")
        kill_path.unlink()
        directory = os.open(root, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return {
            "status": "cleared",
            "incident_id": kill["incident_id"],
            "incident": str(incident_path),
        }
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {"status": "manual_required", "reason": str(exc)}


def cmd_promote(args: argparse.Namespace) -> int:
    release = _read_index_release_state(args.index_root)
    if release is None or release["status"] != CANARY_PENDING:
        raise ValueError("promotion requires a canary_pending release state")
    if release["candidate_generation"] != args.generation:
        raise ValueError("promotion generation does not match pending release")
    current = resolve_current_generation(args.index_root)
    previous = read_pointer(args.index_root, "PREVIOUS")
    if current != args.generation or previous != release["baseline_generation"]:
        raise ValueError("promotion release does not match CURRENT/PREVIOUS")
    candidate_context = _generation_shadow_context(
        args.index_root, args.generation
    )
    baseline_context = _generation_shadow_context(args.index_root, previous)
    if (
        release.get("candidate_artifact_sha256")
        != candidate_context["artifact_sha256"]
        or release.get("baseline_artifact_sha256")
        != baseline_context["artifact_sha256"]
    ):
        raise ValueError("promotion release artifact identity changed")
    shadow_path, shadow_hash = _stable_release_input_hash(
        args.shadow_journal, "promotion shadow journal"
    )
    shadow = summarize_shadow(
        shadow_path,
        args.generation,
        window_days=7,
        generation_context=candidate_context,
    )
    failures = list(shadow["failure_reasons"]) + list(shadow["canary_failure_reasons"])
    if not shadow["ready"] or not shadow["canary_ready"]:
        raise ValueError("promotion requires ready shadow and canary: " + ",".join(failures))
    if sha256_file(shadow_path) != shadow_hash:
        raise ValueError("promotion shadow journal changed during release validation")
    promoted = promote_release(
        _release_state_path(args.index_root), generation_id=args.generation
    )
    kill_switch = _clear_proven_release_kill_switch(args.index_root, promoted)
    _json(
        {
            "status": "open",
            "generation_id": args.generation,
            "release_state": promoted,
            "shadow": shadow,
            "kill_switch": kill_switch,
        }
    )
    return 0


def cmd_rollback(args: argparse.Namespace) -> int:
    _json(rollback_generation(args.index_root))
    return 0


def _parse_guard_time(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} timestamp is required")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{label} timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("guard metrics contain a non-finite or unsupported value") from exc


def _hashed_json(path: Path, label: str) -> tuple[dict[str, Any], str, Path]:
    candidate = path.expanduser().absolute()
    if candidate.is_symlink() or not candidate.is_file():
        raise ValueError(f"{label} must be a regular non-symlink file: {candidate}")
    payload = candidate.read_bytes()
    try:
        value = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} is not valid JSON: {candidate}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object: {candidate}")
    return value, hashlib.sha256(payload).hexdigest(), candidate


def _evaluation_is_fresh(report: dict[str, Any], *, now: datetime, max_age_seconds: float) -> None:
    if not math.isfinite(max_age_seconds) or max_age_seconds <= 0:
        raise ValueError("guard evaluation max age must be finite and positive")
    created = _parse_guard_time(report.get("created_at"), "current evaluation created_at")
    age = (now - created).total_seconds()
    if age < -60.0 or age > max_age_seconds:
        raise ValueError("current guard evaluation is stale or future-dated")


def _generation_guard_binding(
    index_root: Path,
    expected: GenerationConfig,
) -> dict[str, Any]:
    root = index_root.expanduser()
    # Guarding must remain reachable specifically when CURRENT is corrupt.
    # Bind the raw durable pointers first; production resolution intentionally
    # rejects this state and therefore cannot be used as the guard entrypoint.
    generation_id = read_raw_pointer(root, "CURRENT")
    previous_id = read_raw_pointer(root, "PREVIOUS")
    if generation_id == previous_id:
        raise ValueError("CURRENT and PREVIOUS must not reference the same generation")
    generation_dir = root / "generations" / generation_id
    manifest_path = generation_dir / "manifest.json"
    manifest_safe = not manifest_path.is_symlink() and manifest_path.is_file()
    manifest_hash = (
        sha256_file(manifest_path)
        if manifest_safe
        else None
    )
    integrity = verify_generation(generation_dir, full=True)
    expected_report = verify_generation(generation_dir, expected=expected, full=True)
    if manifest_hash is not None and (
        manifest_path.is_symlink()
        or not manifest_path.is_file()
        or sha256_file(manifest_path) != manifest_hash
    ):
        raise ValueError("CURRENT generation manifest changed during guard verification")
    mismatch_names = {
        "model_id_mismatch",
        "model_revision_mismatch",
        "reranker_model_id_mismatch",
        "reranker_revision_mismatch",
        "dimension_mismatch",
        "dtype_mismatch",
        "normalized_mismatch",
        "query_template_mismatch",
        "document_template_mismatch",
        "chunker_version_mismatch",
        "payload_version_mismatch",
        "hard_max_tokens_mismatch",
    }
    expected_errors = set(expected_report.get("errors") or [])
    integrity_errors = [str(value) for value in integrity.get("errors") or []]
    if not manifest_safe:
        integrity_errors.append("manifest_unsafe_or_missing")
    stats = integrity.get("stats") if isinstance(integrity.get("stats"), dict) else {}
    row_count = stats.get("row_count")
    vector_count = stats.get("vectors")
    raw_coverage = stats.get("coverage")
    if isinstance(raw_coverage, bool):
        raise ValueError("generation verification produced invalid coverage")
    if isinstance(raw_coverage, (int, float)):
        coverage = float(raw_coverage)
    elif (
        isinstance(row_count, int)
        and not isinstance(row_count, bool)
        and row_count > 0
        and isinstance(vector_count, int)
        and not isinstance(vector_count, bool)
    ):
        coverage = vector_count / row_count
    else:
        coverage = 0.0
    if not 0.0 <= coverage <= 1.0:
        raise ValueError("generation verification produced invalid coverage")
    return {
        "id": generation_id,
        "previous_id": previous_id,
        "model_id": expected.model_id,
        "model_revision": expected.model_revision,
        "reranker_model_id": expected.reranker_model_id,
        "reranker_revision": expected.reranker_revision,
        "dimension": expected.dimension,
        "manifest_sha256": manifest_hash,
        "identity_valid": manifest_safe
        and bool(integrity.get("generation_id"))
        and not bool(expected_errors.intersection(mismatch_names)),
        "manifest_valid": manifest_safe and bool(integrity.get("ok")),
        "verification_errors": integrity_errors,
        "coverage": coverage,
    }


def _guard_input(path: Path, digest: str, label: str) -> dict[str, str]:
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise ValueError(f"{label} SHA-256 is invalid")
    return {"path": str(path.expanduser().absolute()), "sha256": digest}


_STABLE_ARTIFACT_FIELDS = (
    "generation_id",
    "manifest_hash",
    "catalog_hash",
    "vectors_hash",
)


def _require_guard_evaluation_artifact(
    report: dict[str, Any],
    generation_dir: Path,
    generation_id: str,
    label: str,
) -> dict[str, str]:
    sealed = report.get("artifact_identity")
    required = {
        "schema",
        "generation_id",
        "manifest_sha256",
        "manifest_hash",
        "catalog_hash",
        "vectors_hash",
    }
    if not isinstance(sealed, dict) or set(sealed) != required:
        raise ValueError(f"{label} guard evaluation artifact identity is invalid")
    if sealed.get("generation_id") != generation_id:
        raise ValueError(f"{label} guard evaluation artifact generation mismatch")
    live = generation_artifact_identity(
        generation_dir,
        expected_generation_id=generation_id,
    )
    sealed_stable = {field: str(sealed.get(field) or "") for field in _STABLE_ARTIFACT_FIELDS}
    live_stable = {field: live[field] for field in _STABLE_ARTIFACT_FIELDS}
    if sealed_stable != live_stable:
        raise ValueError(f"{label} guard evaluation artifact changed")
    return live_stable


def _compose_guard_snapshot(
    *,
    index_root: Path,
    expected: GenerationConfig,
    runtime_config_path: Path,
    current_evaluation_path: Path,
    baseline_evaluation_path: Path,
    shadow_journal_path: Path,
    generated_at: datetime,
    max_evaluation_age_seconds: float,
) -> dict[str, Any]:
    current_report, current_hash, current_path = _hashed_json(
        current_evaluation_path, "current guard evaluation"
    )
    baseline_report, baseline_hash, baseline_path = _hashed_json(
        baseline_evaluation_path, "baseline guard evaluation"
    )
    _evaluation_is_fresh(
        current_report,
        now=generated_at,
        max_age_seconds=max_evaluation_age_seconds,
    )
    regression = evaluation_regression_metrics(current_report, baseline_report)
    binding = _generation_guard_binding(index_root, expected)
    previous = read_pointer(index_root.expanduser(), "PREVIOUS")
    if regression["current_generation_id"] != binding["id"]:
        raise ValueError("current evaluation is not bound to CURRENT")
    if regression["baseline_generation_id"] != previous:
        raise ValueError("baseline evaluation is not bound to PREVIOUS")
    generation_context: dict[str, Any] | None = None
    if binding["manifest_valid"] is True and binding["identity_valid"] is True:
        generations_root = index_root.expanduser() / "generations"
        current_identity = _require_guard_evaluation_artifact(
            current_report,
            generations_root / binding["id"],
            binding["id"],
            "current",
        )
        baseline_identity = _require_guard_evaluation_artifact(
            baseline_report,
            generations_root / previous,
            previous,
            "baseline",
        )
        regression["current_artifact_identity"] = current_identity
        regression["baseline_artifact_identity"] = baseline_identity
        generation_context = _generation_shadow_context(index_root, binding["id"])

    journal = shadow_journal_path.expanduser().absolute()
    if journal.is_symlink() or not journal.is_file():
        raise ValueError(f"shadow journal must be a regular non-symlink file: {journal}")
    journal_hash = sha256_file(journal)
    shadow = guard_shadow_metrics(
        journal,
        binding["id"],
        now=generated_at,
        generation_context=generation_context,
    )
    if sha256_file(journal) != journal_hash:
        raise ValueError("shadow journal changed during guard snapshot generation")

    runtime_path = runtime_config_path.expanduser().absolute()
    if runtime_path.is_symlink() or not runtime_path.is_file():
        raise ValueError("runtime config must be a regular non-symlink file")
    runtime_hash = sha256_file(runtime_path)
    snapshot = {
        "schema": GUARD_METRICS_SCHEMA,
        "generated_at": generated_at.astimezone(timezone.utc).isoformat(),
        "generation": {key: value for key, value in binding.items() if key != "coverage"},
        "verification": {"coverage": binding["coverage"]},
        "evaluation": regression,
        "runtime": {
            "query_duplicates": shadow["query_duplicates"],
            "reranker": shadow["reranker"],
            "p95_windows": shadow["p95_windows"],
        },
        "source_links": shadow["source_links"],
        "inputs": {
            "runtime_config": _guard_input(runtime_path, runtime_hash, "runtime config"),
            "current_evaluation": _guard_input(current_path, current_hash, "current evaluation"),
            "baseline_evaluation": _guard_input(
                baseline_path, baseline_hash, "baseline evaluation"
            ),
            "shadow_journal": _guard_input(journal, journal_hash, "shadow journal"),
        },
    }

    def require_unchanged(path: Path, digest: str, label: str) -> None:
        if path.is_symlink() or not path.is_file() or sha256_file(path) != digest:
            raise ValueError(f"{label} changed during guard snapshot generation")

    require_unchanged(current_path, current_hash, "current guard evaluation")
    require_unchanged(baseline_path, baseline_hash, "baseline guard evaluation")
    require_unchanged(journal, journal_hash, "shadow journal")
    require_unchanged(runtime_path, runtime_hash, "runtime config")
    final_binding = _generation_guard_binding(index_root, expected)
    if _canonical_json(final_binding) != _canonical_json(binding):
        raise ValueError("CURRENT generation changed during guard snapshot generation")
    if read_pointer(index_root.expanduser(), "PREVIOUS") != previous:
        raise ValueError("PREVIOUS changed during guard snapshot generation")
    return snapshot


def validate_guard_metrics(
    metrics: dict[str, Any],
    *,
    index_root: Path,
    expected: GenerationConfig,
    runtime_config_path: Path,
    now: datetime | None = None,
    max_age_seconds: float = GUARD_METRICS_MAX_AGE_SECONDS,
    max_evaluation_age_seconds: float = GUARD_EVALUATION_MAX_AGE_SECONDS,
) -> dict[str, Any]:
    if not isinstance(metrics, dict) or metrics.get("schema") != GUARD_METRICS_SCHEMA:
        raise ValueError(f"guard metrics schema must be {GUARD_METRICS_SCHEMA}")
    if not math.isfinite(max_age_seconds) or max_age_seconds <= 0:
        raise ValueError("guard metrics max age must be finite and positive")
    current_time = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    generated = _parse_guard_time(metrics.get("generated_at"), "guard metrics generated_at")
    age = (current_time - generated).total_seconds()
    if age < -60.0 or age > max_age_seconds:
        raise ValueError("guard metrics are stale or future-dated")
    inputs = metrics.get("inputs")
    if not isinstance(inputs, dict):
        raise ValueError("guard metrics inputs must be an object")

    def input_path(name: str) -> Path:
        value = inputs.get(name)
        if not isinstance(value, dict) or set(value) != {"path", "sha256"}:
            raise ValueError(f"guard metrics input {name} is incomplete")
        if not isinstance(value["path"], str) or not Path(value["path"]).is_absolute():
            raise ValueError(f"guard metrics input {name} path must be absolute")
        if not isinstance(value["sha256"], str):
            raise ValueError(f"guard metrics input {name} SHA-256 is invalid")
        return Path(value["path"])

    runtime_path = input_path("runtime_config")
    if runtime_path != runtime_config_path.expanduser().absolute():
        raise ValueError("guard metrics runtime config path does not match this invocation")
    expected_snapshot = _compose_guard_snapshot(
        index_root=index_root,
        expected=expected,
        runtime_config_path=runtime_path,
        current_evaluation_path=input_path("current_evaluation"),
        baseline_evaluation_path=input_path("baseline_evaluation"),
        shadow_journal_path=input_path("shadow_journal"),
        generated_at=generated,
        max_evaluation_age_seconds=max_evaluation_age_seconds,
    )
    if _canonical_json(metrics) != _canonical_json(expected_snapshot):
        raise ValueError("guard metrics do not match their authoritative inputs or CURRENT")
    return expected_snapshot


def cmd_guard_snapshot(args: argparse.Namespace) -> int:
    expected, _runtime, _model_path = _runtime_generation_config(args)
    output = args.output or args.index_root / "guard" / "metrics.json"
    metrics = _compose_guard_snapshot(
        index_root=args.index_root,
        expected=expected,
        runtime_config_path=args.runtime_config,
        current_evaluation_path=args.current_evaluation,
        baseline_evaluation_path=args.baseline_evaluation,
        shadow_journal_path=args.shadow_journal,
        generated_at=datetime.now(timezone.utc),
        max_evaluation_age_seconds=float(args.max_evaluation_age_seconds),
    )
    final_expected, _final_runtime, _final_model_path = _runtime_generation_config(args)
    validate_guard_metrics(
        metrics,
        index_root=args.index_root,
        expected=final_expected,
        runtime_config_path=args.runtime_config,
        max_evaluation_age_seconds=float(args.max_evaluation_age_seconds),
    )
    _durable_json(output, metrics)
    _json({"status": "written", "output": str(output), "metrics": metrics})
    return 0


def _read_regular_json(path: Path) -> dict[str, Any] | None:
    if not path.exists() and not path.is_symlink():
        return None
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"guard state must be a regular non-symlink file: {path}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"guard state must be a JSON object: {path}")
    return value


def _durable_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if (path.exists() or path.is_symlink()) and (path.is_symlink() or not path.is_file()):
        raise ValueError(f"refusing unsafe guard state path: {path}")
    temporary = path.parent / f".{path.name}.tmp.{os.getpid()}"
    if temporary.exists() or temporary.is_symlink():
        raise ValueError(f"refusing existing guard temporary path: {temporary}")
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        try:
            payload = (json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
            remaining = memoryview(payload)
            while remaining:
                written = os.write(descriptor, remaining)
                if written <= 0:
                    raise OSError("guard state write made no progress")
                remaining = remaining[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary.exists() and not temporary.is_symlink():
            temporary.unlink()


def _execute_guard_rollback(
    index_root: Path,
    metrics: dict[str, Any],
    reasons: list[str],
) -> dict[str, Any]:
    root = index_root.expanduser()
    metrics_hash = hashlib.sha256(_canonical_json(metrics).encode("utf-8")).hexdigest()
    kill_path = root / RETRIEVAL_KILL_SWITCH
    # A rollback guard is expected to run when CURRENT may be unreadable or
    # corrupt, so use the raw durable pointer.  ``rollback_generation`` will
    # independently full-verify PREVIOUS before publishing it.
    current = read_raw_pointer(root, "CURRENT")
    previous = read_raw_pointer(root, "PREVIOUS")
    try:
        failed_generation = str(metrics["generation"]["id"])
    except (KeyError, TypeError) as exc:
        raise ValueError("guard rollback requires validated canonical metrics") from exc
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", failed_generation) is None:
        raise ValueError("guard metrics contain an unsafe generation id")
    metrics_previous = metrics["generation"].get("previous_id")
    if metrics_previous is not None and metrics_previous != previous:
        raise ValueError("guard metrics PREVIOUS binding no longer matches")
    incident_path = root / ROLLBACK_INCIDENT_DIR / f"{failed_generation}.json"
    incident = _read_regular_json(incident_path)

    # Read and migrate the former single-slot incident only when it describes
    # this failed generation.  Keep the legacy file so history is never erased.
    if incident is None:
        legacy = _read_regular_json(root / ROLLBACK_INCIDENT)
        if legacy is not None and legacy.get("failed_generation") == failed_generation:
            incident = legacy
            _durable_json(incident_path, incident)

    if incident is not None:
        recorded_failed = str(incident.get("failed_generation") or "")
        recorded_safe = str(incident.get("safe_generation") or "")
        if recorded_failed != failed_generation or not recorded_safe:
            raise ValueError("rollback incident generation binding is invalid")
        if incident.get("status") not in {"prepared", "complete"}:
            raise ValueError("rollback incident status is invalid")
        if {current, previous} != {recorded_failed, recorded_safe}:
            raise ValueError("rollback incident pointers diverged; refusing a blind swap")
        if incident.get("status") == "complete":
            if not kill_path.exists() and not kill_path.is_symlink():
                _durable_json(
                    kill_path,
                    {
                        "schema": "chatdaily-knowledge-retrieval-kill.v1",
                        "disabled": True,
                        "incident_id": incident.get("incident_id"),
                        "reasons": incident.get("reasons", reasons),
                        "recorded_at": shadow_utc_now(),
                    },
                )
            elif kill_path.is_symlink():
                raise ValueError("retrieval kill switch must not be a symlink")
            return {
                "status": "already_rolled_back",
                "incident_id": incident.get("incident_id"),
                "safe_generation": recorded_safe,
                "retrieval_disabled": True,
            }

    if current != failed_generation and incident is None:
        raise ValueError("guard metrics no longer target CURRENT")
    if incident is None and (kill_path.exists() or kill_path.is_symlink()):
        raise ValueError("retrieval kill switch must be explicitly cleared before a new incident")

    if incident is None:
        incident = {
            "schema": "chatdaily-knowledge-rollback-incident.v1",
            "incident_id": metrics_hash,
            "metrics_hash": metrics_hash,
            "failed_generation": current,
            "safe_generation": previous,
            "reasons": reasons,
            "status": "prepared",
            "prepared_at": shadow_utc_now(),
        }
        _durable_json(incident_path, incident)
    else:
        failed = str(incident.get("failed_generation") or "")
        safe = str(incident.get("safe_generation") or "")
        if not failed or not safe or {current, previous} != {failed, safe}:
            raise ValueError("rollback incident pointers diverged; refusing a blind swap")

    _durable_json(
        kill_path,
        {
            "schema": "chatdaily-knowledge-retrieval-kill.v1",
            "disabled": True,
            "incident_id": incident["incident_id"],
            "reasons": incident["reasons"],
            "recorded_at": shadow_utc_now(),
        },
    )
    safe_generation = str(incident["safe_generation"])
    if current == safe_generation:
        rollback = {
            "status": "already_at_safe_generation",
            "generation_id": safe_generation,
        }
    else:
        rollback = rollback_generation(
            root,
            failed_generation_id=str(incident["failed_generation"]),
            safe_generation_id=safe_generation,
        )
        if rollback.get("generation_id") != safe_generation:
            raise ValueError("rollback did not activate the incident safe generation")
    incident["status"] = "complete"
    incident["completed_at"] = shadow_utc_now()
    _durable_json(incident_path, incident)
    return {
        "status": "rolled_back",
        "incident_id": incident["incident_id"],
        "safe_generation": safe_generation,
        "retrieval_disabled": True,
        "rollback": rollback,
    }


def cmd_guard(args: argparse.Namespace) -> int:
    metrics = _read_regular_json(args.metrics)
    if metrics is None:
        raise ValueError("guard metrics file is missing")
    expected, _runtime, _model_path = _runtime_generation_config(args)
    metrics = validate_guard_metrics(
        metrics,
        index_root=args.index_root,
        expected=expected,
        runtime_config_path=args.runtime_config,
        max_age_seconds=float(args.max_age_seconds),
        max_evaluation_age_seconds=float(args.max_evaluation_age_seconds),
    )
    reasons = rollback_reasons(metrics)
    result: dict[str, Any] = {"rollback_required": bool(reasons), "reasons": reasons}
    if reasons and args.execute:
        final_expected, _final_runtime, _final_model_path = _runtime_generation_config(args)
        metrics = validate_guard_metrics(
            metrics,
            index_root=args.index_root,
            expected=final_expected,
            runtime_config_path=args.runtime_config,
            max_age_seconds=float(args.max_age_seconds),
            max_evaluation_age_seconds=float(args.max_evaluation_age_seconds),
        )
        result["rollback"] = _execute_guard_rollback(
            args.index_root,
            metrics,
            reasons,
        )
    _json(result)
    return 2 if reasons and not args.execute else 0


def cmd_status(args: argparse.Namespace) -> int:
    pointers: dict[str, str | None] = {}
    try:
        pointers["CURRENT"] = resolve_current_generation(args.index_root)
    except ValueError:
        pointers["CURRENT"] = None
    try:
        pointers["PREVIOUS"] = read_pointer(args.index_root, "PREVIOUS")
    except ValueError:
        pointers["PREVIOUS"] = None
    generations = []
    root = args.index_root / "generations"
    if root.is_dir():
        for path in sorted(root.iterdir()):
            if not path.is_dir() or not (path / "manifest.json").is_file():
                continue
            manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
            generations.append(
                {
                    "generation_id": manifest.get("generation_id"),
                    "status": manifest.get("status"),
                    "row_count": manifest.get("row_count"),
                    "model_id": manifest.get("model_id"),
                    "model_revision": manifest.get("model_revision"),
                    "reranker_model_id": manifest.get("reranker_model_id"),
                    "reranker_revision": manifest.get("reranker_revision"),
                }
            )
    _json(
        {
            "pointers": pointers,
            "generations": generations,
            "release_state": _read_index_release_state(args.index_root),
        }
    )
    return 0


def _add_runtime(parser: argparse.ArgumentParser, *, default_timeout: float = 180.0) -> None:
    parser.add_argument("--runtime-config", type=Path, default=DEFAULT_RUNTIME_CONFIG)
    parser.add_argument("--endpoint")
    parser.add_argument("--model-revision", default="")
    parser.add_argument("--reranker-revision", default="")
    parser.add_argument("--dimension", type=int, default=DIMENSION)
    parser.add_argument("--batch-size", type=int, default=16, choices=range(1, 33))
    parser.add_argument("--timeout", type=float, default=default_timeout)


def _add_sources(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--data-root", type=Path, default=Path("~/chat-daily").expanduser())
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--chat-db", type=Path)
    parser.add_argument("--sent-ledger", type=Path)
    parser.add_argument("--media-ledger", type=Path)
    parser.add_argument("--podcast-root", type=Path)
    parser.add_argument("--feedback", type=Path)
    parser.add_argument("--feedback-reclassifications", type=Path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="chat-daily-knowledge",
        description="Build, validate and query the side-band ChatDaily Qwen index",
    )
    parser.add_argument("--index-root", type=Path, default=Path("~/chat-daily/index").expanduser())
    commands = parser.add_subparsers(dest="command", required=True)

    scan = commands.add_parser("scan", help="Read and validate all fact sources without writing")
    _add_sources(scan)
    scan.set_defaults(handler=cmd_scan)

    build = commands.add_parser("build", help="Build or resume a Qwen generation")
    _add_sources(build)
    _add_runtime(build)
    build.add_argument("--generation")
    build.add_argument("--resume", action="store_true")
    build.set_defaults(handler=cmd_build)

    verify = commands.add_parser("verify", help="Validate manifest/catalog/vector integrity")
    _add_runtime(verify)
    verify.add_argument("--generation")
    verify.add_argument("--full", action="store_true")
    verify.add_argument("--manifest-only", action="store_true")
    verify.set_defaults(handler=cmd_verify)

    query = commands.add_parser("query", help="Exact + FTS5 + dense + rerank retrieval")
    _add_runtime(query, default_timeout=8.0)
    query.add_argument("query")
    query.add_argument("--generation")
    query.add_argument("--top-k", type=int, default=8, choices=range(1, 51))
    query.add_argument("--no-rerank", action="store_true")
    query.add_argument(
        "--config",
        type=Path,
        default=Path("~/chat-daily/config.yaml").expanduser(),
    )
    query.add_argument("--enable-retrieval", action="store_true")
    query.add_argument(
        "--request-id",
        help="Stable request identity for authoritative telemetry deduplication",
    )
    query.add_argument("--journal", type=Path)
    query.set_defaults(handler=cmd_query)

    diagnostic_query = commands.add_parser(
        "diagnostic-query",
        help="Query one explicit generation without production authorization or telemetry",
    )
    _add_runtime(diagnostic_query, default_timeout=8.0)
    diagnostic_query.add_argument("query")
    diagnostic_query.add_argument("--generation", required=True)
    diagnostic_query.add_argument("--top-k", type=int, default=8, choices=range(1, 51))
    diagnostic_query.add_argument("--no-rerank", action="store_true")
    diagnostic_query.set_defaults(handler=cmd_diagnostic_query)

    task = commands.add_parser("task", help="Recall read/delivered content or inspect recent progress")
    _add_runtime(task, default_timeout=8.0)
    task.add_argument("task", choices=["recall", "progress"])
    task.add_argument("query")
    task.add_argument("--diagnostic", action="store_true")
    task.add_argument("--generation")
    task.add_argument("--top-k", type=int, default=50, choices=range(1, 51))
    task.add_argument("--no-rerank", action="store_true")
    task.add_argument("--config", type=Path, default=Path("~/chat-daily/config.yaml").expanduser())
    task.add_argument("--enable-retrieval", action="store_true")
    task.add_argument("--request-id")
    task.add_argument("--journal", type=Path)
    task.add_argument("--feedback", type=Path, required=True)
    task.add_argument("--delivered-ledger", type=Path, required=True)
    task.add_argument("--expand-archive", action="store_true")
    task.add_argument("--event-root", type=Path)
    task.set_defaults(handler=cmd_task)

    canary = commands.add_parser(
        "canary-query",
        help="Route a deterministic read-only cohort to a candidate generation",
    )
    _add_runtime(canary, default_timeout=8.0)
    canary.add_argument("query")
    canary.add_argument("--request-id", required=True)
    canary.add_argument("--candidate", required=True)
    canary.add_argument("--baseline")
    canary.add_argument("--percent", type=float, default=10.0)
    canary.add_argument("--top-k", type=int, default=8, choices=range(1, 51))
    canary.add_argument("--no-rerank", action="store_true")
    canary.add_argument(
        "--config",
        type=Path,
        default=Path("~/chat-daily/config.yaml").expanduser(),
    )
    canary.add_argument("--enable-retrieval", action="store_true")
    canary.add_argument("--record", action="store_true")
    canary.add_argument("--journal", type=Path)
    canary.set_defaults(handler=cmd_canary_query)

    evaluate = commands.add_parser("evaluate", help="Run the frozen retrieval gold set")
    _add_runtime(evaluate)
    evaluate.add_argument("gold", type=Path)
    evaluate.add_argument("--generation")
    evaluate.add_argument(
        "--baseline-generation",
        help="Measure baseline then candidate in the same process and frozen query order",
    )
    evaluate.add_argument(
        "--baseline-runtime-config",
        type=Path,
        help="Independent runtime config whose model context matches the baseline manifest",
    )
    evaluate.add_argument("--baseline-endpoint")
    evaluate.add_argument("--output", type=Path)
    evaluate.add_argument("--allow-small", action="store_true")
    evaluate.add_argument("--baseline-e2e-p95-ms", type=float)
    evaluate.set_defaults(handler=cmd_evaluate)

    bootstrap = commands.add_parser(
        "bootstrap",
        help="Seed one verified inert baseline when CURRENT/PREVIOUS are absent",
    )
    _add_runtime(bootstrap)
    bootstrap.add_argument("generation")
    bootstrap.set_defaults(handler=cmd_bootstrap)

    activate = commands.add_parser(
        "activate", help="Activate a verified generation as canary_pending"
    )
    _add_runtime(activate)
    activate.add_argument("generation")
    activate.add_argument("--evaluation", type=Path, required=True)
    activate.add_argument("--shadow-journal", type=Path, required=True)
    activate.set_defaults(handler=cmd_activate)

    promote = commands.add_parser(
        "promote", help="Open a CURRENT generation after its canary gates pass"
    )
    promote.add_argument("generation")
    promote.add_argument("--shadow-journal", type=Path, required=True)
    promote.set_defaults(handler=cmd_promote)

    rollback = commands.add_parser("rollback", help="Atomically swap CURRENT/PREVIOUS")
    rollback.set_defaults(handler=cmd_rollback)

    guard_snapshot = commands.add_parser(
        "guard-snapshot",
        help="Atomically derive a complete rollback snapshot from authoritative inputs",
    )
    _add_runtime(guard_snapshot)
    guard_snapshot.add_argument("--current-evaluation", type=Path, required=True)
    guard_snapshot.add_argument("--baseline-evaluation", type=Path, required=True)
    guard_snapshot.add_argument("--shadow-journal", type=Path, required=True)
    guard_snapshot.add_argument("--output", type=Path)
    guard_snapshot.add_argument(
        "--max-evaluation-age-seconds",
        type=float,
        default=GUARD_EVALUATION_MAX_AGE_SECONDS,
    )
    guard_snapshot.set_defaults(handler=cmd_guard_snapshot)

    guard = commands.add_parser("guard", help="Evaluate automatic rollback metrics")
    _add_runtime(guard)
    guard.add_argument("metrics", type=Path)
    guard.add_argument("--execute", action="store_true")
    guard.add_argument(
        "--max-age-seconds",
        type=float,
        default=GUARD_METRICS_MAX_AGE_SECONDS,
    )
    guard.add_argument(
        "--max-evaluation-age-seconds",
        type=float,
        default=GUARD_EVALUATION_MAX_AGE_SECONDS,
    )
    guard.set_defaults(handler=cmd_guard)

    status = commands.add_parser("status", help="List pointers and generation metadata")
    status.set_defaults(handler=cmd_status)

    shadow_record = commands.add_parser(
        "shadow-record", help="Reject legacy manual release-evidence input"
    )
    shadow_record.add_argument("event", type=Path)
    shadow_record.add_argument("--journal", type=Path)
    shadow_record.set_defaults(handler=cmd_shadow_record)

    shadow_status = commands.add_parser(
        "shadow-status", help="Evaluate the seven-day shadow readiness gates"
    )
    shadow_status.add_argument("generation")
    shadow_status.add_argument("--journal", type=Path)
    shadow_status.add_argument("--window-days", type=int, default=7)
    shadow_status.set_defaults(handler=cmd_shadow_status)

    shadow_probe = commands.add_parser(
        "shadow-probe", help="Probe runtime readiness and append a shadow health sample"
    )
    shadow_probe.add_argument("generation")
    shadow_probe.add_argument("--runtime-config", type=Path, default=DEFAULT_RUNTIME_CONFIG)
    shadow_probe.add_argument("--endpoint")
    shadow_probe.add_argument("--timeout", type=float, default=5.0)
    shadow_probe.add_argument("--journal", type=Path)
    shadow_probe.set_defaults(handler=cmd_shadow_probe)

    shadow_audit = commands.add_parser(
        "shadow-audit-sources",
        help="Append authoritative source-link and incremental-freshness audits",
    )
    shadow_audit.add_argument("generation")
    shadow_audit.add_argument("--journal", type=Path)
    _add_sources(shadow_audit)
    shadow_audit.set_defaults(handler=cmd_shadow_audit_sources)

    incremental_refresh = commands.add_parser(
        "incremental-refresh",
        help="Rebuild changed trusted facts into a fresh fully verified generation",
    )
    incremental_refresh.add_argument("shadow_candidate")
    incremental_refresh.add_argument("--output-generation", required=True)
    incremental_refresh.add_argument("--journal", type=Path)
    _add_sources(incremental_refresh)
    _add_runtime(incremental_refresh)
    incremental_refresh.set_defaults(handler=cmd_incremental_refresh)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"chat-daily-knowledge: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
