"""Rebuildable Qwen generation index for ChatDaily knowledge retrieval.

This module is deliberately side-band: it only reads business sources and
writes ``index/generations``.  It has no dependency on Telegram senders,
``seen`` stores, delivery markers, or ledger writers.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import math
import os
import queue
import re
import sqlite3
import stat
import tempfile
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
import numpy as np
from tokenizers import Tokenizer

from chat_daily_tg.inference_queue import (
    CrossProcessInferenceQueue,
    InferencePriorityYield,
    InferenceQueueError,
)


log = logging.getLogger(__name__)

SCHEMA_VERSION = 2
DIMENSION = 4096
EMBEDDING_MODEL = "qwen3-vl-embedding-8b-4bit"
RERANKER_MODEL = "qwen3-vl-reranker-2b-4bit"
QUERY_TEMPLATE = "query-v1"
DOCUMENT_TEMPLATE = "document-v1"
CHUNKER_VERSION = "chatdaily-chunker-v3"
PAYLOAD_VERSION = "text-v1"
HARD_MAX_TOKENS = 800
TARGET_MIN_TOKENS = 450
TARGET_MAX_TOKENS = 700
RRF_K = 60
ONLINE_QUERY_TIMEOUT = 8.0
RERANK_MAX_TOKENS = 1024
RERANK_TOKEN_RESERVE = 32
RERANK_BUCKET_SIZE = 4
UNIT_NORM_TOLERANCE = 1e-4
_GENERATION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_SWITCH_LOCK = ".switch.lock"
_SWITCH_JOURNAL = ".switch-journal.json"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def sha256_file(path: Path, *, block_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(block_size):
            digest.update(block)
    return digest.hexdigest()


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_temp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    temp = Path(raw_temp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        _fsync_directory(path.parent)
    finally:
        if temp.exists():
            temp.unlink()


def _fsync_directory(path: Path) -> None:
    """Durably publish a rename/unlink in *path* on local filesystems."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        # Some filesystems do not support directory fsync.  The file itself was
        # still flushed before replacement, so retain portability here.
        pass
    finally:
        os.close(fd)


def _validate_generation_id(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("generation id must be a string")
    if (
        not value
        or value in {".", ".."}
        or Path(value).is_absolute()
        or "/" in value
        or "\\" in value
        or _GENERATION_ID.fullmatch(value) is None
    ):
        raise ValueError(f"invalid generation id: {value!r}")
    return value


def _validate_generation_directory(generation_dir: Path) -> Path:
    generation_dir = generation_dir.expanduser()
    _validate_generation_id(generation_dir.name)
    if generation_dir.is_symlink() or generation_dir.parent.is_symlink():
        raise ValueError(f"generation directory must not be a symlink: {generation_dir}")
    return generation_dir


def _normalize_vector(vector: Any, dimension: int) -> np.ndarray:
    array = np.asarray(vector, dtype=np.float32)
    if array.shape != (dimension,) or not np.isfinite(array).all():
        raise QwenRuntimeError(f"embedding response shape/finite check failed: {array.shape}")
    norm = float(np.linalg.norm(array.astype(np.float64, copy=False)))
    if not math.isfinite(norm) or norm <= np.finfo(np.float32).tiny:
        raise QwenRuntimeError("embedding vector has zero or nonfinite norm")
    normalized = np.asarray(array / norm, dtype=np.float32)
    if not np.isfinite(normalized).all():
        raise QwenRuntimeError("normalized embedding vector is nonfinite")
    return normalized


def canonical_url(value: str | None) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""
    raw = raw.rstrip(".,;:!?)]}>，。；：！？）】》")
    parts = urlsplit(raw)
    if not parts.scheme or not parts.netloc:
        return raw
    query = [
        (key, item)
        for key, item in parse_qsl(parts.query, keep_blank_values=True)
        if not (
            key.casefold().startswith("utm_")
            or key.casefold() == "spm"
            or key.casefold() == "from"
            or key.casefold().startswith("share_")
        )
    ]
    scheme = "https" if parts.scheme.casefold() == "http" else parts.scheme
    return urlunsplit(
        (scheme, parts.netloc, parts.path, urlencode(query, doseq=True), parts.fragment)
    )


@dataclass(frozen=True)
class SourceLink:
    chat_id: int
    message_id: int
    thread_id: int | None = None
    source_message_id: int | None = None
    ledger_schema: str = ""
    confirmed: bool = True


@dataclass(frozen=True)
class AssetRecord:
    asset_id: str
    sha256: str = ""
    perceptual_hash: str = ""
    mime: str = ""
    width: int | None = None
    height: int | None = None
    local_ref: str = ""
    ocr_text: str = ""
    vision_json: dict[str, Any] = field(default_factory=dict)
    active: bool = True


@dataclass(frozen=True)
class SourceRepresentation:
    """One independently rendered text representation of stable content."""

    text: str
    document_role: str
    representation_type: str
    modality: str = "text"
    source_kind: str = ""
    source_ref: str = ""
    title: str = ""
    locator: str = ""


@dataclass
class SourceDocument:
    content_id: str
    source_kind: str
    source_ref: str
    text: str
    title: str = ""
    published_at: str = ""
    canonical_url: str = ""
    producer: str = ""
    delivery_state: str = ""
    authority: str = "source"
    mapping_status: str = "unmapped"
    metadata: dict[str, Any] = field(default_factory=dict)
    document_role: str = "original"
    modality: str = "text"
    representation_type: str = "source_text"
    source_links: list[SourceLink] = field(default_factory=list)
    assets: list[AssetRecord] = field(default_factory=list)
    alternate_representations: tuple[SourceRepresentation, ...] = ()
    active: bool = True

    @property
    def content_hash(self) -> str:
        if self.alternate_representations:
            representations = [
                {
                    "text": self.text.strip(),
                    "document_role": self.document_role,
                    "representation_type": self.representation_type,
                    "modality": self.modality,
                    "source_kind": self.source_kind,
                    "source_ref": self.source_ref,
                    "title": self.title,
                    "locator": "primary",
                },
                *(asdict(value) for value in self.alternate_representations),
            ]
            return sha256_text(canonical_json(sorted(representations, key=canonical_json)))
        return sha256_text(self.text.strip())


@dataclass(frozen=True)
class ArchiveMessage:
    """One structured archive message with stable source provenance."""

    timestamp: datetime
    sender: str
    body: str
    member_id: str


@dataclass(frozen=True)
class ChunkRecord:
    chunk_id: str
    content_id: str
    locator: str
    ordinal: int
    text: str
    text_hash: str
    modality: str
    representation_type: str
    document_role: str
    start_time_ms: int | None
    end_time_ms: int | None
    member_ids: tuple[str, ...]
    rendered_text: str
    active: bool = True


@dataclass(frozen=True)
class GenerationConfig:
    model_id: str = EMBEDDING_MODEL
    model_revision: str = ""
    dimension: int = DIMENSION
    dtype: str = "float32"
    normalized: bool = True
    query_template: str = QUERY_TEMPLATE
    document_template: str = DOCUMENT_TEMPLATE
    chunker_version: str = CHUNKER_VERSION
    payload_version: str = PAYLOAD_VERSION
    reranker_model_id: str = RERANKER_MODEL
    reranker_revision: str = ""
    hard_max_tokens: int = HARD_MAX_TOKENS

    def identity(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def context_hash(self) -> str:
        return sha256_text(canonical_json(self.identity()))


@dataclass(frozen=True)
class SearchHit:
    rank: int
    content_id: str
    chunk_id: str
    title: str
    source_kind: str
    source_ref: str
    canonical_url: str
    published_at: str
    authority: str
    mapping_status: str
    text: str
    locator: str
    rrf_score: float
    rerank_score: float | None
    channels: tuple[str, ...]
    member_ids: tuple[str, ...]
    source_links: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class PrecomputedQueryEmbedding:
    """One revision-bound query embedding produced before reader construction.

    The reader revalidates every context field and the vector itself before it
    may touch the sealed matrix.  ``error_type`` represents a completed failed
    attempt and intentionally prevents a second outbound embedding request.
    """

    query: str
    rendered_query: str
    generation_id: str
    manifest_hash: str
    model_id: str
    model_revision: str
    dimension: int
    query_template: str
    vector: np.ndarray | None
    elapsed_ms: float
    error_type: str | None = None


class TokenCounter:
    """Exact local Qwen tokenizer with offset-preserving chunk windows."""

    def __init__(self, tokenizer_path: Path):
        path = tokenizer_path
        if path.is_dir():
            path = path / "tokenizer.json"
        if not path.is_file():
            raise FileNotFoundError(f"Qwen tokenizer not found: {path}")
        self.path = path
        self.tokenizer = Tokenizer.from_file(str(path))

    def count(self, text: str) -> int:
        return len(self.tokenizer.encode(text, add_special_tokens=True).ids)

    def windows(
        self,
        text: str,
        *,
        target_tokens: int = 650,
        hard_tokens: int = HARD_MAX_TOKENS,
        overlap_tokens: int = 65,
    ) -> list[str]:
        clean = text.strip()
        if not clean:
            return []
        encoded = self.tokenizer.encode(clean, add_special_tokens=False)
        if len(encoded.ids) <= hard_tokens:
            return [clean]
        offsets = encoded.offsets
        result: list[str] = []
        start_token = 0
        while start_token < len(offsets):
            end_token = min(len(offsets), start_token + min(target_tokens, hard_tokens))
            start_char = offsets[start_token][0]
            end_char = offsets[end_token - 1][1]
            if end_token < len(offsets):
                window = clean[start_char:end_char]
                floor = max(0, int(len(window) * 0.65))
                cuts = [
                    window.rfind("\n\n", floor),
                    window.rfind("\n", floor),
                    window.rfind("。", floor),
                    window.rfind(". ", floor),
                ]
                cut = max(cuts)
                if cut > floor:
                    end_char = start_char + cut + 1
                    while end_token > start_token + 1 and offsets[end_token - 1][1] > end_char:
                        end_token -= 1
            item = clean[start_char:end_char].strip()
            if item:
                result.append(item)
            if end_token >= len(offsets):
                break
            start_token = max(start_token + 1, end_token - overlap_tokens)
        return result


class QwenRuntimeError(RuntimeError):
    pass


class QwenRuntimeBusy(QwenRuntimeError):
    """Retryable saturation/deadline error from the shared inference worker."""


@dataclass(order=True)
class _InferenceJob:
    priority: int
    sequence: int
    task: Callable[[], Any] = field(compare=False)
    deadline: float = field(compare=False)
    done: threading.Event = field(default_factory=threading.Event, compare=False)
    cancelled: threading.Event = field(default_factory=threading.Event, compare=False)
    result: Any = field(default=None, compare=False)
    error: BaseException | None = field(default=None, compare=False)


class _RuntimeWorkQueue:
    """Local worker plus cross-process priority admission for one runtime."""

    def __init__(self, endpoint: str, capacity: int):
        self.capacity = capacity
        self._queue: queue.PriorityQueue[_InferenceJob] = queue.PriorityQueue(maxsize=capacity)
        self._shared_queue = CrossProcessInferenceQueue(endpoint, capacity=capacity)
        self._sequence = 0
        self._sequence_lock = threading.Lock()
        self._worker = threading.Thread(
            target=self._run,
            name="chatdaily-qwen-inference",
            daemon=True,
        )
        self._worker.start()

    def _next_sequence(self) -> int:
        with self._sequence_lock:
            value = self._sequence
            self._sequence += 1
            return value

    def submit(
        self,
        task: Callable[[], Any],
        *,
        online: bool,
        deadline: float,
    ) -> Any:
        if deadline <= time.monotonic():
            raise QwenRuntimeBusy("Qwen inference deadline exhausted before queueing")
        job = _InferenceJob(0 if online else 1, self._next_sequence(), task, deadline)
        try:
            if online:
                self._queue.put_nowait(job)
            else:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise QwenRuntimeBusy(
                        "Qwen offline inference deadline exhausted before queueing"
                    )
                self._queue.put(job, timeout=remaining)
        except queue.Full as exc:
            mode = "online" if online else "offline"
            raise QwenRuntimeBusy(f"Qwen {mode} inference queue is full") from exc

        remaining = deadline - time.monotonic()
        if remaining <= 0 or not job.done.wait(remaining):
            job.cancelled.set()
            raise QwenRuntimeBusy("Qwen inference deadline exhausted in queue")
        if job.error is not None:
            raise job.error
        return job.result

    def _run(self) -> None:
        while True:
            job = self._queue.get()
            completed = True
            try:
                if not job.cancelled.is_set():
                    try:
                        with self._shared_queue.acquire(
                            online=job.priority == 0,
                            deadline=job.deadline,
                            yield_to_online=(
                                None
                                if job.priority == 0
                                else lambda: any(
                                    queued.priority < job.priority
                                    for queued in tuple(self._queue.queue)
                                )
                            ),
                        ):
                            if not job.cancelled.is_set():
                                job.result = job.task()
                    except InferencePriorityYield:
                        completed = False
                        if not job.cancelled.is_set():
                            self._queue.put_nowait(job)
                    except BaseException as exc:  # preserve the caller's exception contract
                        if isinstance(exc, InferenceQueueError):
                            job.error = QwenRuntimeBusy(str(exc))
                        else:
                            job.error = exc
            finally:
                if completed:
                    job.done.set()
                self._queue.task_done()


_RUNTIME_QUEUES: dict[tuple[int, str], _RuntimeWorkQueue] = {}
_RUNTIME_QUEUES_LOCK = threading.Lock()


def _runtime_work_queue(endpoint: str, capacity: int) -> _RuntimeWorkQueue:
    key = (os.getpid(), endpoint.rstrip("/"))
    with _RUNTIME_QUEUES_LOCK:
        existing = _RUNTIME_QUEUES.get(key)
        if existing is not None:
            if existing.capacity != capacity:
                raise ValueError(
                    "inference queue capacity conflicts with the existing runtime worker"
                )
            return existing
        created = _RuntimeWorkQueue(endpoint, capacity)
        _RUNTIME_QUEUES[key] = created
        return created


class QwenRuntimeClient:
    """Strict client for the loopback Qwen embedding and reranking runtime."""

    RETRYABLE = {429, 500, 502, 503, 504}

    def __init__(
        self,
        endpoint: str = "http://127.0.0.1:8790/v1",
        *,
        embedding_model: str = EMBEDDING_MODEL,
        embedding_revision: str = "",
        reranker_model: str = RERANKER_MODEL,
        reranker_revision: str = "",
        dimension: int = DIMENSION,
        batch_size: int = 16,
        queue_capacity: int = 64,
        timeout: float = 180.0,
        token: str = "",
    ):
        if not 1 <= batch_size <= 32:
            raise ValueError("embedding batch_size must be 1..32")
        if not 1 <= queue_capacity <= 1024:
            raise ValueError("inference queue_capacity must be 1..1024")
        self.endpoint = endpoint.rstrip("/")
        self.embedding_model = embedding_model
        self.embedding_revision = embedding_revision
        self.reranker_model = reranker_model
        self.reranker_revision = reranker_revision
        self.dimension = dimension
        self.batch_size = batch_size
        self.timeout = timeout
        self.headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._runtime_queue = _runtime_work_queue(self.endpoint, queue_capacity)

    @staticmethod
    def _require_revision_attestation(
        payload: dict[str, Any],
        field: str,
        expected: str,
        label: str,
    ) -> None:
        if not isinstance(expected, str) or not expected.strip():
            raise QwenRuntimeError(f"{label} client expected revision is missing")
        actual = payload.get(field)
        if not isinstance(actual, str) or not actual.strip():
            raise QwenRuntimeError(f"{label} response revision attestation is missing")
        if actual != expected:
            raise QwenRuntimeError(f"{label} response revision mismatch")

    def health(self) -> dict[str, Any]:
        with httpx.Client(timeout=min(self.timeout, 20.0), trust_env=False) as client:
            response = client.get(f"{self.endpoint}/health", headers=self.headers)
            response.raise_for_status()
            result = response.json()
        if not isinstance(result, dict) or not result.get("ready"):
            raise QwenRuntimeError(f"Qwen runtime not ready: {result!r}")
        embedding_health = result.get("embedding")
        reranker_health = result.get("reranker")
        if not isinstance(embedding_health, dict) or not embedding_health.get("ready"):
            raise QwenRuntimeError("Qwen embedding model is not ready")
        if not isinstance(reranker_health, dict) or not reranker_health.get("ready"):
            raise QwenRuntimeError("Qwen reranker model is not ready")
        self._require_revision_attestation(
            result,
            "embedding_revision",
            self.embedding_revision,
            "embedding health",
        )
        self._require_revision_attestation(
            result,
            "reranker_revision",
            self.reranker_revision,
            "reranker health",
        )
        return result

    def embed_documents(self, texts: Sequence[str]) -> np.ndarray:
        return self._embed(texts, timeout=self.timeout, online=False)

    def embed_queries(self, texts: Sequence[str], *, timeout: float | None = None) -> np.ndarray:
        return self._embed(texts, timeout=timeout, online=True)

    def _embed(
        self,
        texts: Sequence[str],
        *,
        timeout: float | None = None,
        online: bool,
    ) -> np.ndarray:
        if not texts:
            return np.empty((0, self.dimension), dtype=np.float32)
        effective_timeout = self.timeout if timeout is None else min(self.timeout, timeout)
        if effective_timeout <= 0:
            raise QwenRuntimeError("Qwen embedding deadline exhausted")
        deadline = time.monotonic() + effective_timeout
        output: list[np.ndarray] = []
        with httpx.Client(timeout=effective_timeout, trust_env=False) as client:
            for start in range(0, len(texts), self.batch_size):
                batch = list(texts[start : start + self.batch_size])
                payload = [{"text": value} for value in batch]

                def request() -> dict[str, Any]:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise QwenRuntimeBusy("Qwen embedding deadline exhausted in queue")
                    return self._post_retry(
                        client,
                        "/embeddings",
                        {"model": self.embedding_model, "input": payload},
                        timeout=remaining,
                    )

                data = self._runtime_queue.submit(
                    request,
                    online=online,
                    deadline=deadline,
                )
                if data.get("model") != self.embedding_model:
                    raise QwenRuntimeError("embedding response model mismatch")
                self._require_revision_attestation(
                    data,
                    "revision",
                    self.embedding_revision,
                    "embedding",
                )
                items = data.get("data") if isinstance(data, dict) else None
                if not isinstance(items, list) or len(items) != len(batch):
                    raise QwenRuntimeError(
                        f"embedding response count mismatch: {len(items or [])} != {len(batch)}"
                    )
                ordered: list[np.ndarray | None] = [None] * len(batch)
                for item in items:
                    if not isinstance(item, dict):
                        raise QwenRuntimeError("embedding response item is not an object")
                    index = item.get("index")
                    vector = item.get("embedding")
                    if (
                        not isinstance(index, int)
                        or isinstance(index, bool)
                        or not 0 <= index < len(batch)
                        or ordered[index] is not None
                        or not isinstance(vector, list)
                    ):
                        raise QwenRuntimeError("embedding response has invalid/duplicate index")
                    ordered[index] = _normalize_vector(vector, self.dimension)
                if any(item is None for item in ordered):
                    raise QwenRuntimeError("embedding response has a missing index")
                output.extend(item for item in ordered if item is not None)
        return np.stack(output).astype(np.float32, copy=False)

    def rerank(
        self,
        query: str,
        documents: Sequence[str],
        *,
        top_n: int,
        timeout: float | None = None,
        document_token_counts: Sequence[int] | None = None,
    ) -> list[dict[str, Any]]:
        if not documents:
            return []
        if len(documents) > 64:
            raise ValueError("reranker candidates must be <=64")
        requested_top_n = min(top_n, len(documents))
        if requested_top_n <= 0:
            raise ValueError("reranker top_n must be positive")
        if document_token_counts is not None:
            if len(document_token_counts) != len(documents):
                raise ValueError("reranker token counts must match candidates")
            if any(
                not isinstance(value, int) or isinstance(value, bool) or value <= 0
                for value in document_token_counts
            ):
                raise ValueError("reranker token counts must be positive integers")
        effective_timeout = self.timeout if timeout is None else min(self.timeout, timeout)
        if effective_timeout <= 0:
            raise QwenRuntimeError("Qwen reranker deadline exhausted")
        deadline = time.monotonic() + effective_timeout
        if document_token_counts is None:
            buckets = [list(range(len(documents)))]
            bucket_top_ns = [requested_top_n]
        else:
            ordered = sorted(
                range(len(documents)),
                key=lambda index: (document_token_counts[index], index),
            )
            buckets = [
                ordered[start : start + RERANK_BUCKET_SIZE]
                for start in range(0, len(ordered), RERANK_BUCKET_SIZE)
            ]
            # Scores are comparable per query/document pair, so every bucket
            # must return every candidate before the one global top_n cut.
            bucket_top_ns = [len(bucket) for bucket in buckets]

        clean: list[dict[str, Any]] = []
        with httpx.Client(timeout=effective_timeout, trust_env=False) as client:
            for bucket, bucket_top_n in zip(buckets, bucket_top_ns, strict=True):
                bucket_documents = [documents[index] for index in bucket]

                def request() -> dict[str, Any]:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise QwenRuntimeBusy("Qwen reranker deadline exhausted in queue")
                    return self._post_retry(
                        client,
                        "/rerank",
                        {
                            "model": self.reranker_model,
                            "query": query,
                            "documents": bucket_documents,
                            "top_n": bucket_top_n,
                        },
                        timeout=remaining,
                    )

                data = self._runtime_queue.submit(
                    request,
                    online=True,
                    deadline=deadline,
                )
                if data.get("model") != self.reranker_model:
                    raise QwenRuntimeError("reranker response model mismatch")
                self._require_revision_attestation(
                    data,
                    "revision",
                    self.reranker_revision,
                    "reranker",
                )
                rows = data.get("results") if isinstance(data, dict) else None
                if not isinstance(rows, list):
                    raise QwenRuntimeError("reranker response has no results list")
                if len(rows) != bucket_top_n:
                    raise QwenRuntimeError(
                        f"reranker response count mismatch: {len(rows)} != {bucket_top_n}"
                    )
                seen: set[int] = set()
                for row in rows:
                    if not isinstance(row, dict):
                        raise QwenRuntimeError("reranker result is not an object")
                    local_index = row.get("index")
                    score = row.get("relevance_score")
                    if (
                        not isinstance(local_index, int)
                        or isinstance(local_index, bool)
                        or not 0 <= local_index < len(bucket)
                        or local_index in seen
                        or not isinstance(score, (int, float))
                        or not math.isfinite(float(score))
                    ):
                        raise QwenRuntimeError("reranker result has invalid index/score")
                    seen.add(local_index)
                    clean.append(
                        {
                            "index": bucket[local_index],
                            "relevance_score": float(score),
                        }
                    )
        if document_token_counts is None:
            return clean
        clean.sort(key=lambda row: (-row["relevance_score"], row["index"]))
        return clean[:requested_top_n]

    def _post_retry(
        self,
        client: httpx.Client,
        route: str,
        payload: dict[str, Any],
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        last: Exception | None = None
        effective_timeout = self.timeout if timeout is None else min(self.timeout, timeout)
        deadline = time.monotonic() + effective_timeout
        for attempt in range(3):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                response = client.post(
                    f"{self.endpoint}{route}",
                    headers=self.headers,
                    json=payload,
                    timeout=max(0.001, remaining),
                )
                if response.status_code in {400, 401, 403, 404, 413, 422}:
                    raise QwenRuntimeError(f"Qwen {route} permanent HTTP {response.status_code}")
                response.raise_for_status()
                body = response.json()
                if not isinstance(body, dict):
                    raise QwenRuntimeError(f"Qwen {route} response is not an object")
                return body
            except QwenRuntimeError:
                raise
            except httpx.HTTPStatusError as exc:
                last = exc
                if exc.response.status_code not in self.RETRYABLE:
                    raise QwenRuntimeError(f"Qwen {route} HTTP {exc.response.status_code}") from exc
            except (httpx.TimeoutException, httpx.ConnectError) as exc:
                last = exc
            if attempt < 2:
                delay = min(0.5 * (2**attempt), max(0.0, deadline - time.monotonic()))
                if delay:
                    time.sleep(delay)
        raise QwenRuntimeError(f"Qwen {route} failed after 3 attempts") from last


def render_query(query: str, *, template: str = QUERY_TEMPLATE) -> str:
    if template != QUERY_TEMPLATE:
        raise ValueError(f"unsupported query template: {template}")
    return f"[任务] 检索与下列查询相关的 ChatDaily 内容\n[查询]\n{query.strip()}"


def render_document(
    document: SourceDocument,
    text: str,
    *,
    start: str = "",
    end: str = "",
    template: str = DOCUMENT_TEMPLATE,
) -> str:
    if template != DOCUMENT_TEMPLATE:
        raise ValueError(f"unsupported document template: {template}")
    lines = [
        f"[来源类型] {document.source_kind}",
        f"[来源名称] {document.source_ref}",
    ]
    if start or end or document.published_at:
        range_text = " - ".join(value for value in (start, end) if value)
        lines.append(f"[时间] {range_text or document.published_at}")
    if document.title:
        lines.append(f"[标题] {document.title}")
    lines.extend((f"[文档角色] {document.document_role}", "[正文]", text.strip()))
    return "\n".join(lines)


_MESSAGE_HEADER = re.compile(r"^###\s+(\d{4}-\d{2}-\d{2})\s+([0-2]\d:[0-5]\d)\s*$")
_SENDER_LINE = re.compile(r"^\*\*(.+?)\*\*:\s*(.*)$")
_TELEGRAM_HEADER = re.compile(
    r"^\[Telegram / (?P<group>.+?) / (?P<hm>[0-2]\d:[0-5]\d) / "
    r"(?P<sender>.+?)\](?: (?P<forward>\[转发\]))?(?: (?P<body>.*))?$"
)
_TELEGRAM_EXPORT_FOOTER = re.compile(r"^>\s*跳过空文本/低信息消息\s+\d+\s+条\s*$")
_SRT_TIME = re.compile(
    r"^(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s+-->\s+"
    r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})"
)


def _time_ms(parts: Sequence[str]) -> int:
    hours, minutes, seconds, millis = (int(value) for value in parts)
    return ((hours * 60 + minutes) * 60 + seconds) * 1000 + millis


def parse_srt(text: str) -> list[tuple[int, int, str]]:
    cues: list[tuple[int, int, str]] = []
    blocks = re.split(r"\r?\n\s*\r?\n", text.strip())
    for block in blocks:
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        timing_index = next((i for i, line in enumerate(lines) if _SRT_TIME.match(line)), -1)
        if timing_index < 0:
            continue
        match = _SRT_TIME.match(lines[timing_index])
        assert match is not None
        body = " ".join(lines[timing_index + 1 :]).strip()
        if body:
            cues.append((_time_ms(match.groups()[:4]), _time_ms(match.groups()[4:]), body))
    return cues


def _safe_locator(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip())[:240]


def chunk_document(document: SourceDocument, counter: TokenCounter) -> list[ChunkRecord]:
    """Apply source-aware v1 chunking and validate the rendered 800-token cap."""
    return chunk_document_with_config(document, counter, GenerationConfig())


def chunk_document_with_config(
    document: SourceDocument, counter: TokenCounter, config: GenerationConfig
) -> list[ChunkRecord]:
    """Configured form used by builders; the public wrapper keeps v1 defaults."""
    if not document.alternate_representations:
        return _chunk_document_representation(document, counter, config)

    representations = [
        SourceRepresentation(
            text=document.text,
            document_role=document.document_role,
            representation_type=document.representation_type,
            modality=document.modality,
            source_kind=document.source_kind,
            source_ref=document.source_ref,
            title=document.title,
            locator="primary",
        ),
        *document.alternate_representations,
    ]
    output: list[ChunkRecord] = []
    for representation in representations:
        view = replace(
            document,
            text=representation.text,
            document_role=representation.document_role,
            representation_type=representation.representation_type,
            modality=representation.modality,
            source_kind=representation.source_kind or document.source_kind,
            source_ref=representation.source_ref or document.source_ref,
            title=representation.title or document.title,
            alternate_representations=(),
        )
        identity = canonical_json(
            {
                "locator": representation.locator,
                "representation_type": representation.representation_type,
                "document_role": representation.document_role,
                "source_kind": view.source_kind,
                "source_ref": view.source_ref,
            }
        )
        prefix = (
            f"representation:{_safe_locator(representation.representation_type)}:"
            f"{sha256_text(identity)[:16]}"
        )
        for chunk in _chunk_document_representation(view, counter, config):
            locator = f"{prefix}/{chunk.locator}"
            output.append(
                replace(
                    chunk,
                    chunk_id=sha256_text(
                        "|".join(
                            (
                                document.content_id,
                                locator,
                                chunk.text_hash,
                                config.chunker_version,
                            )
                        )
                    ),
                    locator=locator,
                    ordinal=len(output),
                )
            )
    return output


def _chunk_document_representation(
    document: SourceDocument, counter: TokenCounter, config: GenerationConfig
) -> list[ChunkRecord]:
    if not document.text.strip():
        return []
    if document.representation_type == "srt":
        specs = _chunk_srt(document, counter)
    elif document.source_kind in {"wechat_archive", "telegram_archive"}:
        specs = _chunk_conversation(document, counter)
    else:
        specs = _chunk_long_text(document, counter)
    metadata_members = document.metadata.get("source_message_ids", [])
    stable_members = (
        tuple(f"source-message:{value}" for value in metadata_members)
        if isinstance(metadata_members, list)
        else ()
    )
    output: list[ChunkRecord] = []
    for ordinal, spec in enumerate(specs):
        raw_text, locator, start_ms, end_ms, members = spec
        rendered = render_document(
            document,
            raw_text,
            start=str(start_ms or ""),
            end=str(end_ms or ""),
            template=config.document_template,
        )
        if counter.count(rendered) > config.hard_max_tokens:
            # Source-aware grouping gets a final exact-token safety split.  The
            # caller keeps locators and member ids so every fragment can link
            # back to the same source span.
            overhead = counter.count(
                render_document(
                    document,
                    "",
                    start=str(start_ms or ""),
                    end=str(end_ms or ""),
                    template=config.document_template,
                )
            )
            # Leave a small tokenizer margin for boundary/special-token effects
            # in window implementations. The exact rendered count below remains
            # the final authority.
            hard_budget = max(64, config.hard_max_tokens - overhead - 8)
            parts = counter.windows(
                raw_text,
                target_tokens=min(
                    hard_budget,
                    max(64, TARGET_MAX_TOKENS - overhead - 8),
                ),
                hard_tokens=hard_budget,
                overlap_tokens=50,
            )
        else:
            parts = [raw_text]
        for part_index, part in enumerate(parts):
            part_locator = locator if len(parts) == 1 else f"{locator}#part-{part_index + 1}"
            rendered_part = render_document(
                document,
                part,
                start=str(start_ms or ""),
                end=str(end_ms or ""),
                template=config.document_template,
            )
            tokens = counter.count(rendered_part)
            if tokens > config.hard_max_tokens:
                raise ValueError(
                    f"chunk still exceeds {config.hard_max_tokens} tokens: "
                    f"{document.content_id} {part_locator} ({tokens})"
                )
            text_hash = sha256_text(part)
            chunk_id = sha256_text(
                "|".join((document.content_id, part_locator, text_hash, config.chunker_version))
            )
            output.append(
                ChunkRecord(
                    chunk_id=chunk_id,
                    content_id=document.content_id,
                    locator=part_locator,
                    ordinal=len(output),
                    text=part,
                    text_hash=text_hash,
                    modality=document.modality,
                    representation_type=document.representation_type,
                    document_role=document.document_role,
                    start_time_ms=start_ms,
                    end_time_ms=end_ms,
                    member_ids=tuple(dict.fromkeys((*members, *stable_members))),
                    rendered_text=rendered_part,
                    active=document.active,
                )
            )
    return output


def _chunk_long_text(
    document: SourceDocument, counter: TokenCounter
) -> list[tuple[str, str, int | None, int | None, tuple[str, ...]]]:
    overhead = counter.count(render_document(document, ""))
    windows = counter.windows(
        document.text,
        target_tokens=max(64, 650 - overhead),
        hard_tokens=max(64, HARD_MAX_TOKENS - overhead),
        overlap_tokens=65,
    )
    return [
        (item, f"text:{index + 1}", None, None, ())
        for index, item in enumerate(windows)
        if item.strip()
    ]


def _chunk_srt(
    document: SourceDocument, counter: TokenCounter
) -> list[tuple[str, str, int | None, int | None, tuple[str, ...]]]:
    cues = parse_srt(document.text)
    if not cues:
        return _chunk_long_text(document, counter)
    output: list[tuple[str, str, int | None, int | None, tuple[str, ...]]] = []
    start = 0
    while start < len(cues):
        end = start
        parts: list[str] = []
        while end < len(cues):
            parts.append(cues[end][2])
            duration = cues[end][1] - cues[start][0]
            rendered = render_document(
                document, " ".join(parts), start=str(cues[start][0]), end=str(cues[end][1])
            )
            if duration >= 120_000 or counter.count(rendered) >= TARGET_MAX_TOKENS:
                break
            end += 1
        end = min(end, len(cues) - 1)
        body = " ".join(cue[2] for cue in cues[start : end + 1])
        start_ms, end_ms = cues[start][0], cues[end][1]
        members = tuple(f"cue:{index + 1}" for index in range(start, end + 1))
        output.append((body, f"time:{start_ms}-{end_ms}", start_ms, end_ms, members))
        if end >= len(cues) - 1:
            break
        overlap_target = max(start_ms, end_ms - 15_000)
        next_start = end
        while next_start > start and cues[next_start][0] > overlap_target:
            next_start -= 1
        start = max(start + 1, next_start)
    return output


def parse_archive_messages(
    *,
    source_kind: str,
    source_ref: str,
    text: str,
    published_at: str = "",
) -> tuple[ArchiveMessage, ...]:
    """Parse a complete structured archive using stable message identities.

    Identities exclude source line numbers, so header or metadata insertions do
    not invalidate message provenance. Identical duplicates are disambiguated
    by their occurrence in source order.
    """
    lines = text.splitlines()
    messages: list[ArchiveMessage] = []
    identity_occurrences: Counter[str] = Counter()

    def append_message(timestamp: datetime, sender: str, body: str) -> None:
        digest = sha256_text(
            canonical_json(
                {
                    "source_ref": source_ref,
                    "timestamp": timestamp.isoformat(),
                    "sender": sender,
                    "body": body,
                }
            )
        )
        identity_occurrences[digest] += 1
        messages.append(
            ArchiveMessage(
                timestamp=timestamp,
                sender=sender,
                body=body,
                member_id=f"archive-message:v1:{digest}:{identity_occurrences[digest]}",
            )
        )

    if source_kind == "telegram_archive":
        try:
            archive_date = datetime.fromisoformat(published_at).date()
        except (TypeError, ValueError):
            archive_date = None
        if archive_date is None:
            return ()
        index = 0
        while index < len(lines):
            header = _TELEGRAM_HEADER.match(lines[index].strip())
            if not header:
                index += 1
                continue
            cursor = index + 1
            body = [str(header.group("body") or "").strip()]
            while cursor < len(lines) and not _TELEGRAM_HEADER.match(lines[cursor].strip()):
                clean = lines[cursor].strip()
                if clean and not _TELEGRAM_EXPORT_FOOTER.match(clean):
                    body.append(clean)
                cursor += 1
            clean_body = "\n".join(part for part in body if part).strip()
            if header.group("forward") and clean_body:
                clean_body = f"[转发] {clean_body}"
            sender = str(header.group("sender") or "").strip()
            if sender and clean_body:
                hm = str(header.group("hm"))
                timestamp = datetime.fromisoformat(f"{archive_date.isoformat()}T{hm}:00")
                append_message(timestamp, sender, clean_body)
            index = max(cursor, index + 1)
        return tuple(messages)

    if source_kind != "wechat_archive":
        return ()

    index = 0
    while index < len(lines):
        header = _MESSAGE_HEADER.match(lines[index].strip())
        if not header:
            index += 1
            continue
        date_value, hm = header.groups()
        cursor = index + 1
        while cursor < len(lines) and not lines[cursor].strip():
            cursor += 1
        sender = ""
        body: list[str] = []
        if cursor < len(lines):
            sender_line = _SENDER_LINE.match(lines[cursor].strip())
            if sender_line:
                sender, first = sender_line.groups()
                if first:
                    body.append(first)
                cursor += 1
        while cursor < len(lines) and not _MESSAGE_HEADER.match(lines[cursor].strip()):
            if lines[cursor].strip():
                body.append(lines[cursor].strip())
            cursor += 1
        if sender and body:
            append_message(
                datetime.fromisoformat(f"{date_value}T{hm}:00"),
                sender,
                "\n".join(body),
            )
        index = max(cursor, index + 1)
    return tuple(messages)


def split_archive_message_sessions(
    messages: Sequence[ArchiveMessage], *, gap_seconds: int = 600
) -> tuple[tuple[ArchiveMessage, ...], ...]:
    """Split ordered structured messages when the adjacent gap exceeds 10m."""
    sessions: list[tuple[ArchiveMessage, ...]] = []
    current: list[ArchiveMessage] = []
    for message in messages:
        if current and (
            message.timestamp - current[-1].timestamp
        ).total_seconds() > gap_seconds:
            sessions.append(tuple(current))
            current = []
        current.append(message)
    if current:
        sessions.append(tuple(current))
    return tuple(sessions)


def _chunk_conversation(
    document: SourceDocument, counter: TokenCounter
) -> list[tuple[str, str, int | None, int | None, tuple[str, ...]]]:
    messages = list(
        parse_archive_messages(
            source_kind=document.source_kind,
            source_ref=document.source_ref,
            text=document.text,
            published_at=document.published_at,
        )
    )
    if not messages:
        return _chunk_long_text(document, counter)

    groups: list[list[ArchiveMessage]] = []
    current: list[ArchiveMessage] = []
    overlap_only = False
    for message in messages:
        if current and (message.timestamp - current[-1].timestamp).total_seconds() > 600:
            if not overlap_only:
                groups.append(current)
            current = []
            overlap_only = False
        candidate = current + [message]
        body = "\n".join(
            f"[{item.timestamp.strftime('%H:%M')}] {item.sender}：{item.body}"
            for item in candidate
        )
        if (
            current
            and len(current) >= 5
            and (
                len(candidate) > 20
                or counter.count(render_document(document, body)) > TARGET_MAX_TOKENS
            )
        ):
            groups.append(current)
            current = current[-2:] if len(current) > 2 else []
            overlap_only = bool(current)
        current.append(message)
        overlap_only = False
    if current and not overlap_only:
        groups.append(current)

    output = []
    for group in groups:
        body = "\n".join(
            f"[{item.timestamp.strftime('%H:%M')}] {item.sender}：{item.body}"
            for item in group
        )
        start_text = group[0].timestamp.isoformat()
        end_text = group[-1].timestamp.isoformat()
        # Overlapping windows can legitimately share the same timestamp
        # endpoints.  Include stable member provenance in the locator so two
        # different windows (and their final safety-split parts) can never
        # collapse onto the same chunk id.
        member_span = sha256_text(
            canonical_json(
                {
                    "first": group[0].member_id,
                    "last": group[-1].member_id,
                    "count": len(group),
                }
            )
        )[:16]
        output.append(
            (
                body,
                f"messages:{start_text}-{end_text}:{member_span}",
                int(group[0].timestamp.timestamp() * 1000),
                int(group[-1].timestamp.timestamp() * 1000),
                tuple(item.member_id for item in group),
            )
        )
    return output


CATALOG_SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE content_items (
    content_id TEXT PRIMARY KEY,
    source_kind TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    canonical_url TEXT NOT NULL,
    producer TEXT NOT NULL,
    title TEXT NOT NULL,
    published_at TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    delivery_state TEXT NOT NULL,
    authority TEXT NOT NULL,
    mapping_status TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE source_links (
    content_id TEXT NOT NULL REFERENCES content_items(content_id),
    chat_id INTEGER NOT NULL,
    thread_id INTEGER,
    message_id INTEGER NOT NULL,
    source_message_id INTEGER,
    ledger_schema TEXT NOT NULL,
    confirmed INTEGER NOT NULL,
    UNIQUE(ledger_schema, chat_id, message_id)
);
CREATE TABLE chunks (
    chunk_id TEXT PRIMARY KEY,
    content_id TEXT NOT NULL REFERENCES content_items(content_id),
    locator TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    text TEXT NOT NULL,
    text_hash TEXT NOT NULL,
    modality TEXT NOT NULL,
    representation_type TEXT NOT NULL,
    document_role TEXT NOT NULL,
    start_time_ms INTEGER,
    end_time_ms INTEGER,
    member_ids_json TEXT NOT NULL,
    chunker_version TEXT NOT NULL,
    rendered_text TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE assets (
    asset_id TEXT PRIMARY KEY,
    content_id TEXT NOT NULL REFERENCES content_items(content_id),
    sha256 TEXT NOT NULL,
    perceptual_hash TEXT NOT NULL,
    mime TEXT NOT NULL,
    width INTEGER,
    height INTEGER,
    local_ref TEXT NOT NULL,
    ocr_text TEXT NOT NULL,
    vision_json TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE embedding_generations (
    generation_id TEXT PRIMARY KEY,
    baseline_generation_id TEXT,
    baseline_pointer TEXT,
    model_id TEXT NOT NULL,
    model_revision TEXT NOT NULL,
    reranker_model_id TEXT NOT NULL,
    reranker_revision TEXT NOT NULL,
    dimension INTEGER NOT NULL,
    normalized INTEGER NOT NULL,
    query_template TEXT NOT NULL,
    document_template TEXT NOT NULL,
    chunker_version TEXT NOT NULL,
    payload_version TEXT NOT NULL,
    manifest_hash TEXT NOT NULL,
    status TEXT NOT NULL
);
CREATE TABLE vector_rows (
    generation_id TEXT NOT NULL REFERENCES embedding_generations(generation_id),
    chunk_id TEXT NOT NULL REFERENCES chunks(chunk_id),
    row_index INTEGER NOT NULL UNIQUE,
    vector_hash TEXT,
    state TEXT NOT NULL,
    PRIMARY KEY(generation_id, chunk_id)
);
CREATE TABLE ingestion_cursors (
    source_name TEXT PRIMARY KEY,
    cursor_json TEXT NOT NULL,
    last_event_hash TEXT NOT NULL,
    last_success TEXT NOT NULL,
    status TEXT NOT NULL
);
CREATE TABLE feedback_events (
    event_hash TEXT PRIMARY KEY,
    content_id TEXT,
    action TEXT NOT NULL,
    confirmed INTEGER NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE TABLE exact_terms (
    term TEXT NOT NULL,
    kind TEXT NOT NULL,
    content_id TEXT NOT NULL REFERENCES content_items(content_id),
    chunk_id TEXT REFERENCES chunks(chunk_id),
    UNIQUE(term, kind, content_id, chunk_id)
);
CREATE INDEX idx_chunks_content ON chunks(content_id, ordinal);
CREATE INDEX idx_vector_rows_generation ON vector_rows(generation_id, row_index);
CREATE INDEX idx_exact_terms_term ON exact_terms(term);
CREATE VIRTUAL TABLE chunk_fts USING fts5(
    chunk_id UNINDEXED, text, title, source_ref, tokenize='trigram'
);
"""


def _manifest_identity(manifest: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "schema_version",
        "generation_id",
        "baseline_generation_id",
        "baseline_pointer",
        "model_id",
        "model_revision",
        "reranker_model_id",
        "reranker_revision",
        "dimension",
        "dtype",
        "normalized",
        "query_template",
        "document_template",
        "chunker_version",
        "payload_version",
        "hard_max_tokens",
    )
    return {key: manifest.get(key) for key in keys}


def compute_manifest_hash(manifest: dict[str, Any]) -> str:
    return sha256_text(canonical_json(_manifest_identity(manifest)))


def model_revision_fingerprint(model_path: Path) -> str:
    """Fingerprint local weights without rereading multi-GB shards per query.

    Size/mtime alone is insufficient because a same-sized replacement can
    preserve mtime.  APFS inode and ctime change for replacement and in-place
    writes respectively; small model/config files are additionally hashed in
    full.  This keeps online reader startup cheap while detecting the practical
    local weight-mutation cases that would invalidate a generation.
    """
    rows: list[dict[str, Any]] = []
    for path in sorted(model_path.glob("*")):
        if not path.is_file() or path.name.startswith("."):
            continue
        stat_result = path.stat()
        row: dict[str, Any] = {
            "name": path.name,
            "size": stat_result.st_size,
            "mtime_ns": stat_result.st_mtime_ns,
            "ctime_ns": stat_result.st_ctime_ns,
            "inode": stat_result.st_ino,
            "device": stat_result.st_dev,
        }
        if path.suffix in {".json", ".txt"} and stat_result.st_size <= 10_000_000:
            row["sha256"] = sha256_file(path)
        rows.append(row)
    if not rows:
        raise FileNotFoundError(f"embedding model directory is empty: {model_path}")
    return sha256_text(canonical_json(rows))


_MONOTONIC_LEDGER_SOURCES = {"sent_content_ledger", "media_sent_ledger"}
_IMMUTABLE_LEDGER_AUTHORITIES = {"sent-content.v1"}


def _load_ingestion_cursors(catalog: Path) -> dict[str, dict[str, Any]]:
    conn = sqlite3.connect(f"file:{catalog}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT source_name,cursor_json FROM ingestion_cursors ORDER BY source_name"
        ).fetchall()
    finally:
        conn.close()
    output: dict[str, dict[str, Any]] = {}
    for source_name, raw in rows:
        try:
            cursor = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"baseline cursor is malformed for {source_name}") from exc
        if not isinstance(cursor, dict):
            raise ValueError(f"baseline cursor is not an object for {source_name}")
        output[str(source_name)] = cursor
    return output


def _validated_event_hashes(source_name: str, cursor: dict[str, Any]) -> list[str]:
    hashes = cursor.get("event_hashes")
    count = cursor.get("count")
    if (
        cursor.get("mode") != "set"
        or not isinstance(count, int)
        or isinstance(count, bool)
        or not isinstance(hashes, list)
        or len(hashes) != count
        or any(not isinstance(value, str) or not value for value in hashes)
    ):
        raise ValueError(f"{source_name} cursor lacks a complete set inventory")
    normalized = sorted(hashes)
    if cursor.get("set_hash") != sha256_text(canonical_json(normalized)):
        raise ValueError(f"{source_name} cursor set hash does not match its inventory")
    for key in ("delivery_ids", "content_ids", "document_ids"):
        if key not in cursor:
            continue
        values = cursor[key]
        if not isinstance(values, list) or any(
            not isinstance(value, str) or not value for value in values
        ):
            raise ValueError(f"{source_name} cursor has an invalid {key} inventory")
    return normalized


def _validate_cursor_monotonicity(
    baseline: dict[str, dict[str, Any]], current: dict[str, dict[str, Any]]
) -> None:
    for source_name in sorted(baseline):
        if source_name not in current:
            raise ValueError(f"baseline source cursor disappeared: {source_name}")
        if source_name not in _MONOTONIC_LEDGER_SOURCES:
            continue
        previous_cursor = baseline[source_name]
        current_cursor = current[source_name]
        previous_hashes = _validated_event_hashes(source_name, previous_cursor)
        current_hashes = _validated_event_hashes(source_name, current_cursor)
        previous_schema = previous_cursor.get("schema")
        current_schema = current_cursor.get("schema")
        if previous_hashes and (
            not isinstance(previous_schema, str)
            or not previous_schema
            or current_schema != previous_schema
        ):
            raise ValueError(f"{source_name} ledger schema changed across generations")
        previous_count = Counter(previous_hashes)
        current_count = Counter(current_hashes)
        missing = previous_count - current_count
        if missing:
            raise ValueError(f"{source_name} ledger was truncated or replaced")
        for key in ("delivery_ids", "content_ids", "document_ids"):
            if key not in previous_cursor:
                continue
            if key not in current_cursor:
                raise ValueError(f"{source_name} cursor lost its {key} inventory")
            previous_values = set(previous_cursor[key])
            current_values = set(current_cursor[key])
            if not previous_values.issubset(current_values):
                raise ValueError(f"{source_name} {key} inventory shrank")


def _load_tombstone_documents(
    catalog: Path, baseline_generation_id: str
) -> dict[str, tuple[SourceDocument, bool, str]]:
    conn = sqlite3.connect(f"file:{catalog}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    output: dict[str, tuple[SourceDocument, bool, str]] = {}
    try:
        items = conn.execute("SELECT * FROM content_items ORDER BY content_id").fetchall()
        for item in items:
            chunks = conn.execute(
                "SELECT * FROM chunks WHERE content_id=? ORDER BY ordinal,chunk_id",
                (item["content_id"],),
            ).fetchall()
            if not chunks:
                raise ValueError(f"baseline content has no chunks: {item['content_id']}")
            try:
                metadata = json.loads(item["metadata_json"])
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"baseline content metadata is malformed: {item['content_id']}"
                ) from exc
            if not isinstance(metadata, dict):
                raise ValueError(
                    f"baseline content metadata is not an object: {item['content_id']}"
                )
            links = [
                SourceLink(
                    chat_id=int(row["chat_id"]),
                    thread_id=(int(row["thread_id"]) if row["thread_id"] is not None else None),
                    message_id=int(row["message_id"]),
                    source_message_id=(
                        int(row["source_message_id"])
                        if row["source_message_id"] is not None
                        else None
                    ),
                    ledger_schema=str(row["ledger_schema"]),
                    confirmed=bool(row["confirmed"]),
                )
                for row in conn.execute(
                    "SELECT * FROM source_links WHERE content_id=? "
                    "ORDER BY ledger_schema,chat_id,message_id",
                    (item["content_id"],),
                )
            ]
            assets = [
                AssetRecord(
                    asset_id=str(row["asset_id"]),
                    sha256=str(row["sha256"]),
                    perceptual_hash=str(row["perceptual_hash"]),
                    mime=str(row["mime"]),
                    width=int(row["width"]) if row["width"] is not None else None,
                    height=int(row["height"]) if row["height"] is not None else None,
                    local_ref=str(row["local_ref"]),
                    ocr_text=str(row["ocr_text"]),
                    vision_json=json.loads(row["vision_json"]),
                    active=False,
                )
                for row in conn.execute(
                    "SELECT * FROM assets WHERE content_id=? ORDER BY asset_id",
                    (item["content_id"],),
                )
            ]
            first_chunk = chunks[0]
            prior_hash = str(item["content_hash"])
            metadata["knowledge_tombstone"] = {
                "baseline_generation_id": baseline_generation_id,
                "previous_content_hash": prior_hash,
                "previous_active": bool(item["active"]),
            }
            document = SourceDocument(
                content_id=str(item["content_id"]),
                source_kind=str(item["source_kind"]),
                source_ref=str(item["source_ref"]),
                text="\n\n".join(str(chunk["text"]) for chunk in chunks),
                title=str(item["title"]),
                published_at=str(item["published_at"]),
                canonical_url=str(item["canonical_url"]),
                producer=str(item["producer"]),
                delivery_state=str(item["delivery_state"]),
                authority=str(item["authority"]),
                mapping_status=str(item["mapping_status"]),
                metadata=metadata,
                document_role=str(first_chunk["document_role"]),
                modality=str(first_chunk["modality"]),
                representation_type=str(first_chunk["representation_type"]),
                source_links=links,
                assets=assets,
                active=False,
            )
            output[document.content_id] = (document, bool(item["active"]), prior_hash)
    finally:
        conn.close()
    return output


class GenerationBuilder:
    def __init__(
        self,
        root: Path,
        config: GenerationConfig,
        counter: TokenCounter,
        client: QwenRuntimeClient,
    ):
        self.root = root.expanduser()
        self.config = config
        self.counter = counter
        self.client = client
        if client.embedding_model != config.model_id:
            raise ValueError("builder client embedding model does not match generation config")
        if str(getattr(client, "embedding_revision", "")) != config.model_revision:
            raise ValueError("builder client embedding revision does not match generation config")
        if client.reranker_model != config.reranker_model_id:
            raise ValueError("builder client reranker model does not match generation config")
        client_reranker_revision = str(getattr(client, "reranker_revision", ""))
        if client_reranker_revision and client_reranker_revision != config.reranker_revision:
            raise ValueError("builder client reranker revision does not match generation config")
        if client.dimension != config.dimension:
            raise ValueError("builder client dimension does not match generation config")
        if config.dtype != "float32" or not config.normalized:
            raise ValueError("KnowledgeIndex v1 requires normalized float32 vectors")
        if config.chunker_version != CHUNKER_VERSION or config.payload_version != PAYLOAD_VERSION:
            raise ValueError("unsupported KnowledgeIndex chunker/payload version")
        render_query("probe", template=config.query_template)
        render_document(
            SourceDocument("probe", "probe", "probe", "probe"),
            "probe",
            template=config.document_template,
        )

    def _fresh_baseline(self) -> tuple[str | None, str | None]:
        current = _strict_optional_pointer(self.root, "CURRENT")
        if current is not None:
            return current, "CURRENT"
        previous = _strict_optional_pointer(self.root, "PREVIOUS")
        if previous is not None:
            return previous, "PREVIOUS"
        return None, None

    def _reconcile_baseline(
        self,
        documents: Sequence[SourceDocument],
        source_cursors: dict[str, dict[str, Any]],
        baseline_generation_id: str | None,
        baseline_pointer: str | None,
    ) -> tuple[SourceDocument, ...]:
        current_by_id: dict[str, SourceDocument] = {}
        for document in documents:
            previous = current_by_id.setdefault(document.content_id, document)
            if previous is not document:
                raise ValueError(f"duplicate current content id: {document.content_id}")
        if baseline_generation_id is None:
            return tuple(documents)

        baseline_dir = _validate_generation_directory(
            self.root / "generations" / baseline_generation_id
        )
        incremental_baseline = baseline_pointer == "INCREMENTAL"
        report = verify_generation(
            baseline_dir,
            expected=self.config if incremental_baseline else None,
            full=incremental_baseline,
        )
        if not report["ok"]:
            raise ValueError(
                f"baseline generation is invalid: {baseline_generation_id}: {report['errors']}"
            )
        baseline_catalog = baseline_dir / "catalog.sqlite"
        baseline_cursors = _load_ingestion_cursors(baseline_catalog)
        _validate_cursor_monotonicity(baseline_cursors, source_cursors)
        baseline_documents = _load_tombstone_documents(baseline_catalog, baseline_generation_id)

        current_delivery_owner: dict[tuple[str, int, int], str] = {}
        for document in documents:
            for link in document.source_links:
                if not link.confirmed or not link.ledger_schema:
                    continue
                key = (link.ledger_schema, link.chat_id, link.message_id)
                previous_owner = current_delivery_owner.setdefault(key, document.content_id)
                if previous_owner != document.content_id:
                    raise ValueError(f"current ledger authority conflict for delivery {key}")

        reconciled = list(documents)
        for content_id, (tombstone, was_active, prior_hash) in baseline_documents.items():
            current = current_by_id.get(content_id)
            if current is not None:
                if was_active and tombstone.authority in _IMMUTABLE_LEDGER_AUTHORITIES:
                    if (
                        not current.active
                        or current.authority != tombstone.authority
                        or current.content_hash != prior_hash
                    ):
                        raise ValueError(
                            f"immutable ledger content changed across generations: {content_id}"
                        )
                for link in tombstone.source_links:
                    if not link.confirmed or not link.ledger_schema:
                        continue
                    owner = current_delivery_owner.get(
                        (link.ledger_schema, link.chat_id, link.message_id)
                    )
                    if owner is not None and owner != content_id:
                        raise ValueError(
                            "ledger delivery changed content authority across generations: "
                            f"{link.ledger_schema}:{link.chat_id}:{link.message_id}"
                        )
                continue
            if was_active and tombstone.authority in _IMMUTABLE_LEDGER_AUTHORITIES:
                raise ValueError(f"immutable ledger content disappeared: {content_id}")
            for link in tombstone.source_links:
                if not link.confirmed or not link.ledger_schema:
                    continue
                owner = current_delivery_owner.get(
                    (link.ledger_schema, link.chat_id, link.message_id)
                )
                if owner is not None and owner != content_id:
                    raise ValueError(
                        "ledger delivery changed content authority across generations: "
                        f"{link.ledger_schema}:{link.chat_id}:{link.message_id}"
                    )
            reconciled.append(tombstone)
        return tuple(reconciled)

    def build(
        self,
        documents: Sequence[SourceDocument],
        *,
        source_cursors: dict[str, dict[str, Any]] | None = None,
        feedback_events: Sequence[dict[str, Any]] = (),
        generation_id: str | None = None,
        resume: bool = False,
        baseline_generation_id: str | None = None,
        baseline_pointer: str | None = None,
    ) -> dict[str, Any]:
        generations = self.root / "generations"
        generations.mkdir(parents=True, exist_ok=True)
        if generation_id is None:
            build_id = datetime.now().strftime("%Y%m%dT%H%M%SZ")
            generation_id = (
                f"qwen-vl-{self.config.dimension}-{build_id}-{self.config.context_hash[:8]}"
            )
        generation_id = _validate_generation_id(generation_id)
        generation_dir = generations / generation_id
        if generation_dir.is_symlink():
            raise ValueError(f"generation directory must not be a symlink: {generation_dir}")
        catalog = generation_dir / "catalog.sqlite"
        vectors = generation_dir / "vectors.f32"
        manifest_path = generation_dir / "manifest.json"
        progress_path = generation_dir / "build-progress.json"
        if generation_dir.exists() and not resume:
            raise FileExistsError(f"generation already exists: {generation_dir}")
        generation_dir.mkdir(parents=True, exist_ok=True)
        _validate_generation_directory(generation_dir)

        prior: dict[str, Any] | None = None
        if resume:
            if not manifest_path.is_file() or not catalog.is_file():
                raise ValueError("resume requires an existing manifest and catalog")
            prior_raw = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(prior_raw, dict):
                raise ValueError("resume manifest must be an object")
            prior = prior_raw
            if not {"baseline_generation_id", "baseline_pointer"} <= prior.keys():
                raise ValueError("resume manifest lacks fixed baseline provenance")
            prior_baseline_generation_id = prior.get("baseline_generation_id")
            prior_baseline_pointer = prior.get("baseline_pointer")
            if (
                baseline_generation_id is not None
                and baseline_generation_id != prior_baseline_generation_id
            ) or (
                baseline_pointer is not None
                and baseline_pointer != prior_baseline_pointer
            ):
                raise ValueError("resume baseline provenance does not match existing generation")
            baseline_generation_id = prior_baseline_generation_id
            baseline_pointer = prior_baseline_pointer
        else:
            if baseline_generation_id is None and baseline_pointer is None:
                baseline_generation_id, baseline_pointer = self._fresh_baseline()
        if (baseline_generation_id is None) != (baseline_pointer is None):
            raise ValueError("baseline generation and pointer provenance must be paired")
        if baseline_generation_id is not None:
            baseline_generation_id = _validate_generation_id(baseline_generation_id)
            if baseline_pointer not in {"CURRENT", "PREVIOUS", "INCREMENTAL"}:
                raise ValueError("baseline pointer provenance is invalid")
            if baseline_generation_id == generation_id:
                raise ValueError("candidate generation cannot use itself as baseline")

        current_cursors = source_cursors or {}
        documents = self._reconcile_baseline(
            documents,
            current_cursors,
            baseline_generation_id,
            baseline_pointer,
        )

        manifest = {
            "schema_version": SCHEMA_VERSION,
            "generation_id": generation_id,
            "baseline_generation_id": baseline_generation_id,
            "baseline_pointer": baseline_pointer,
            **self.config.identity(),
            "created_at": prior.get("created_at") if prior is not None else utc_now(),
            "row_count": 0,
            "content_count": 0,
            "catalog_hash": "",
            "vectors_hash": "",
            "status": "building",
        }
        manifest["manifest_hash"] = compute_manifest_hash(manifest)
        # A fresh build needs the same chunks for provenance and catalog rows.
        # Tokenize once; resume keeps the streaming provenance path so it does
        # not retain every chunk while checking an existing catalog.
        prepared_chunks = (
            [chunk_document_with_config(document, self.counter, self.config)
             for document in documents]
            if not resume else None
        )
        source_provenance = self._source_provenance(
            documents, current_cursors, feedback_events, prepared_chunks=prepared_chunks
        )
        requested_snapshot = sha256_text(canonical_json(source_provenance))
        if resume:
            assert prior is not None
            if prior.get("manifest_hash") != manifest["manifest_hash"]:
                raise ValueError("resume generation context does not match requested context")
            manifest = prior
            if not progress_path.is_file():
                raise ValueError("resume requires build-progress.json")
            progress = json.loads(progress_path.read_text(encoding="utf-8"))
            if (
                progress.get("baseline_generation_id") != baseline_generation_id
                or progress.get("baseline_pointer") != baseline_pointer
            ):
                raise ValueError("resume checkpoint baseline provenance changed")
            if progress.get("source_snapshot") != requested_snapshot:
                raise ValueError("source snapshot changed; refusing unsafe generation resume")
            if progress.get("source_provenance") != source_provenance:
                raise ValueError("source provenance changed; refusing unsafe generation resume")
        else:
            atomic_write_text(
                manifest_path, json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
            )
            self._create_catalog(
                catalog,
                generation_id,
                documents,
                current_cursors,
                feedback_events,
                manifest["manifest_hash"],
                baseline_generation_id=baseline_generation_id,
                baseline_pointer=baseline_pointer,
                prepared_chunks=prepared_chunks,
            )
            if vectors.exists():
                raise FileExistsError(vectors)
            vectors.touch()

        del prepared_chunks
        conn = sqlite3.connect(str(catalog))
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT vr.row_index, vr.chunk_id, c.rendered_text, vr.state "
                "FROM vector_rows vr JOIN chunks c USING(chunk_id) "
                "WHERE vr.generation_id=? ORDER BY vr.row_index",
                (generation_id,),
            ).fetchall()
            cursor_rows = conn.execute(
                "SELECT source_name,cursor_json FROM ingestion_cursors ORDER BY source_name"
            ).fetchall()
            feedback_rows = conn.execute(
                "SELECT payload_json FROM feedback_events ORDER BY event_hash"
            ).fetchall()
            catalog_snapshot = sha256_text(
                canonical_json(
                    {
                        "chunks": [
                            (row["chunk_id"], sha256_text(row["rendered_text"])) for row in rows
                        ],
                        "cursors": [
                            (row["source_name"], json.loads(row["cursor_json"]))
                            for row in cursor_rows
                        ],
                        "feedback": sorted(
                            sha256_text(row["payload_json"]) for row in feedback_rows
                        ),
                    }
                )
            )
            if resume and progress_path.is_file():
                progress = json.loads(progress_path.read_text(encoding="utf-8"))
                if progress.get("catalog_snapshot") != catalog_snapshot:
                    raise ValueError("catalog changed; refusing unsafe generation resume")
            completed = sum(row["state"] == "ready" for row in rows)
            progress_base = {
                "generation_id": generation_id,
                "baseline_generation_id": baseline_generation_id,
                "baseline_pointer": baseline_pointer,
                "total": len(rows),
                "source_snapshot": requested_snapshot,
                "source_provenance": source_provenance,
                "catalog_snapshot": catalog_snapshot,
            }
            atomic_write_text(
                progress_path,
                json.dumps(
                    {
                        **progress_base,
                        "status": "prepared",
                        "completed": completed,
                        "updated_at": utc_now(),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
            )
            self.client.health()
            for start in range(0, len(rows), self.client.batch_size):
                batch_rows = rows[start : start + self.client.batch_size]
                pending = [row for row in batch_rows if row["state"] != "ready"]
                if not pending:
                    continue
                payloads = [row["rendered_text"] for row in pending]
                for payload in payloads:
                    tokens = self.counter.count(payload)
                    if tokens > self.config.hard_max_tokens:
                        raise ValueError(f"document payload exceeds hard token max: {tokens}")
                matrix = self.client.embed_documents(payloads)
                with vectors.open("r+b") as handle, conn:
                    for row, vector in zip(pending, matrix, strict=True):
                        offset = int(row["row_index"]) * self.config.dimension * 4
                        handle.seek(offset)
                        raw = np.asarray(
                            _normalize_vector(vector, self.config.dimension), dtype="<f4"
                        ).tobytes(order="C")
                        handle.write(raw)
                        conn.execute(
                            "UPDATE vector_rows SET vector_hash=?, state='ready' "
                            "WHERE generation_id=? AND chunk_id=?",
                            (sha256_bytes(raw), generation_id, row["chunk_id"]),
                        )
                    handle.flush()
                    os.fsync(handle.fileno())
                completed += len(pending)
                atomic_write_text(
                    progress_path,
                    json.dumps(
                        {
                            **progress_base,
                            "status": "embedding",
                            "completed": completed,
                            "updated_at": utc_now(),
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                    + "\n",
                )
                log.info("knowledge embedding progress: %d/%d", completed, len(rows))
            conn.execute(
                "UPDATE embedding_generations SET status='ready' WHERE generation_id=?",
                (generation_id,),
            )
            conn.commit()
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()

        manifest.update(
            {
                "row_count": len(rows),
                "content_count": len(documents),
                "catalog_hash": sha256_file(catalog),
                "vectors_hash": sha256_file(vectors),
                "status": "ready",
                "completed_at": utc_now(),
            }
        )
        atomic_write_text(manifest_path, json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
        atomic_write_text(
            progress_path,
            json.dumps(
                {
                    **progress_base,
                    "status": "complete",
                    "completed": len(rows),
                    "updated_at": utc_now(),
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
        )
        report = verify_generation(generation_dir, expected=self.config, full=True)
        if not report["ok"]:
            raise ValueError(f"built generation failed verification: {report['errors']}")
        return report

    def _source_provenance(
        self,
        documents: Sequence[SourceDocument],
        source_cursors: dict[str, dict[str, Any]],
        feedback_events: Sequence[dict[str, Any]],
        *,
        prepared_chunks: Sequence[Sequence[ChunkRecord]] | None = None,
    ) -> dict[str, Any]:
        if prepared_chunks is not None and len(prepared_chunks) != len(documents):
            raise ValueError("prepared chunk count does not match documents")
        rows: list[dict[str, Any]] = []
        for index, document in enumerate(documents):
            chunks = (prepared_chunks[index] if prepared_chunks is not None
                      else chunk_document_with_config(document, self.counter, self.config))
            rows.append(
                {
                    "content_id": document.content_id,
                    "source_kind": document.source_kind,
                    "source_ref": document.source_ref,
                    "canonical_url": canonical_url(document.canonical_url),
                    "producer": document.producer,
                    "title": document.title,
                    "published_at": document.published_at,
                    "content_hash": document.content_hash,
                    "delivery_state": document.delivery_state,
                    "authority": document.authority,
                    "mapping_status": document.mapping_status,
                    "metadata": document.metadata,
                    "document_role": document.document_role,
                    "modality": document.modality,
                    "representation_type": document.representation_type,
                    "active": bool(document.active),
                    "source_links": sorted(
                        (asdict(link) for link in document.source_links),
                        key=canonical_json,
                    ),
                    "assets": sorted(
                        (asdict(asset) for asset in document.assets),
                        key=canonical_json,
                    ),
                    "chunks": [
                        {
                            "chunk_id": chunk.chunk_id,
                            "locator": chunk.locator,
                            "ordinal": chunk.ordinal,
                            "text_hash": chunk.text_hash,
                            "rendered_hash": sha256_text(chunk.rendered_text),
                            "representation_type": chunk.representation_type,
                            "document_role": chunk.document_role,
                            "modality": chunk.modality,
                            "member_ids": list(chunk.member_ids),
                            "start_time_ms": chunk.start_time_ms,
                            "end_time_ms": chunk.end_time_ms,
                            "active": bool(chunk.active),
                        }
                        for chunk in chunks
                    ],
                }
            )
        return {
            "documents": rows,
            "cursors": dict(sorted(source_cursors.items())),
            "feedback": sorted(
                (json.loads(canonical_json(event)) for event in feedback_events),
                key=canonical_json,
            ),
        }

    def _create_catalog(
        self,
        path: Path,
        generation_id: str,
        documents: Sequence[SourceDocument],
        source_cursors: dict[str, dict[str, Any]],
        feedback_events: Sequence[dict[str, Any]],
        manifest_hash: str,
        *,
        baseline_generation_id: str | None,
        baseline_pointer: str | None,
        prepared_chunks: Sequence[Sequence[ChunkRecord]] | None = None,
    ) -> None:
        if prepared_chunks is not None and len(prepared_chunks) != len(documents):
            raise ValueError("prepared chunk count does not match documents")
        conn = sqlite3.connect(str(path))
        try:
            conn.executescript(CATALOG_SCHEMA)
            conn.execute(
                "INSERT INTO embedding_generations("
                "generation_id,baseline_generation_id,baseline_pointer,"
                "model_id,model_revision,reranker_model_id,reranker_revision,"
                "dimension,normalized,"
                "query_template,document_template,chunker_version,payload_version,"
                "manifest_hash,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    generation_id,
                    baseline_generation_id,
                    baseline_pointer,
                    self.config.model_id,
                    self.config.model_revision,
                    self.config.reranker_model_id,
                    self.config.reranker_revision,
                    self.config.dimension,
                    int(self.config.normalized),
                    self.config.query_template,
                    self.config.document_template,
                    self.config.chunker_version,
                    self.config.payload_version,
                    manifest_hash,
                    "building",
                ),
            )
        except Exception:
            conn.close()
            raise
        vector_chunks: list[ChunkRecord] = []
        with conn:
            for index, document in enumerate(documents):
                conn.execute(
                    "INSERT INTO content_items VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        document.content_id,
                        document.source_kind,
                        document.source_ref,
                        canonical_url(document.canonical_url),
                        document.producer,
                        document.title,
                        document.published_at,
                        document.content_hash,
                        document.delivery_state,
                        document.authority,
                        document.mapping_status,
                        canonical_json(document.metadata),
                        int(document.active),
                    ),
                )
                for link in document.source_links:
                    conn.execute(
                        "INSERT INTO source_links VALUES(?,?,?,?,?,?,?)",
                        (
                            document.content_id,
                            link.chat_id,
                            link.thread_id,
                            link.message_id,
                            link.source_message_id,
                            link.ledger_schema,
                            int(link.confirmed),
                        ),
                    )
                for asset in document.assets:
                    conn.execute(
                        "INSERT OR REPLACE INTO assets VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            asset.asset_id,
                            document.content_id,
                            asset.sha256,
                            asset.perceptual_hash,
                            asset.mime,
                            asset.width,
                            asset.height,
                            asset.local_ref,
                            asset.ocr_text,
                            canonical_json(asset.vision_json),
                            int(asset.active),
                        ),
                    )
                chunks = (prepared_chunks[index] if prepared_chunks is not None
                          else chunk_document_with_config(document, self.counter, self.config))
                for chunk in chunks:
                    conn.execute(
                        "INSERT INTO chunks VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            chunk.chunk_id,
                            chunk.content_id,
                            chunk.locator,
                            chunk.ordinal,
                            chunk.text,
                            chunk.text_hash,
                            chunk.modality,
                            chunk.representation_type,
                            chunk.document_role,
                            chunk.start_time_ms,
                            chunk.end_time_ms,
                            canonical_json(chunk.member_ids),
                            self.config.chunker_version,
                            chunk.rendered_text,
                            int(chunk.active),
                        ),
                    )
                    vector_chunks.append(chunk)
                    conn.execute(
                        "INSERT INTO chunk_fts(chunk_id,text,title,source_ref) VALUES(?,?,?,?)",
                        (chunk.chunk_id, chunk.text, document.title, document.source_ref),
                    )
                    for term, kind in exact_terms(document, chunk):
                        conn.execute(
                            "INSERT OR IGNORE INTO exact_terms VALUES(?,?,?,?)",
                            (term, kind, document.content_id, chunk.chunk_id),
                        )
            # MLX pads each embedding batch to its longest item.  A stable
            # token-length row order materially reduces wasted work while row
            # identity remains explicit in vector_rows and independent of
            # source/catalog ordering.
            vector_chunks.sort(
                key=lambda chunk: (
                    self.counter.count(chunk.rendered_text),
                    chunk.content_id,
                    chunk.ordinal,
                    chunk.chunk_id,
                )
            )
            for row_index, chunk in enumerate(vector_chunks):
                conn.execute(
                    "INSERT INTO vector_rows VALUES(?,?,?,?,?)",
                    (generation_id, chunk.chunk_id, row_index, None, "pending"),
                )
            for source_name, cursor in source_cursors.items():
                cursor_json = canonical_json(cursor)
                conn.execute(
                    "INSERT INTO ingestion_cursors VALUES(?,?,?,?,?)",
                    (source_name, cursor_json, sha256_text(cursor_json), utc_now(), "ok"),
                )
            for event in feedback_events:
                raw = canonical_json(event)
                conn.execute(
                    "INSERT OR IGNORE INTO feedback_events VALUES(?,?,?,?,?)",
                    (
                        sha256_text(raw),
                        str(event.get("content_id") or "") or None,
                        str(event.get("action") or event.get("intent") or "unknown"),
                        int(
                            bool(
                                event.get("confirmed") or event.get("mapping_status") == "confirmed"
                            )
                        ),
                        raw,
                    ),
                )
        conn.execute("PRAGMA optimize")
        conn.close()


_URL = re.compile(r"https?://[^\s<>()\[\]{}]+", re.IGNORECASE)
_BVID = re.compile(r"\bBV[0-9A-Za-z]{10}\b")
_YOUTUBE = re.compile(r"(?:youtu\.be/|youtube\.com/(?:watch\?v=|shorts/))([\w-]{6,})")
_PRODUCT_MODEL_PATTERNS = (
    re.compile(r"\b(?:RTX|GTX)\s+\d{3,4}(?:\s+(?:Ti|SUPER))?\b", re.IGNORECASE),
    re.compile(r"\bRX\s+\d{3,4}(?:\s+(?:XT|XTX))?\b", re.IGNORECASE),
    re.compile(
        r"\biPhone\s+\d{1,2}(?:\s+(?:Pro(?:\s+Max)?|Plus|Air|Mini|Max))?\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:qwen|gpt|claude|gemini|llama|mistral|deepseek)"
        r"[A-Za-z0-9._-]{2,}\b",
        re.IGNORECASE,
    ),
)
_MODEL_METADATA_KEYS = ("model", "model_id", "models", "model_ids", "product_model")


def _model_identity_values(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        if value.strip():
            yield value.strip()
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            yield from _model_identity_values(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _model_identity_values(item)


def exact_terms(document: SourceDocument, chunk: ChunkRecord) -> Iterator[tuple[str, str]]:
    values: list[tuple[str, str]] = [
        (document.content_id, "content_id"),
        (document.source_ref, "source_ref"),
        (document.canonical_url, "url"),
    ]
    for link in document.source_links:
        if not link.confirmed:
            continue
        values.append((f"{link.chat_id}:{link.message_id}", "message_scoped_confirmed"))
        if link.ledger_schema:
            values.append(
                (
                    f"{link.ledger_schema}:{link.chat_id}:{link.message_id}",
                    "ledger_message_scoped_confirmed",
                )
            )
        if link.source_message_id is not None:
            values.append(
                (
                    f"{link.chat_id}:{link.source_message_id}",
                    "source_message_scoped_confirmed",
                )
            )
            if link.ledger_schema:
                values.append(
                    (
                        f"{link.ledger_schema}:{link.chat_id}:{link.source_message_id}",
                        "ledger_source_message_scoped_confirmed",
                    )
                )
    member_content_ids = document.metadata.get("member_content_ids", [])
    if isinstance(member_content_ids, list):
        values.extend((str(value), "member_content_id") for value in member_content_ids)
    source_message_ids = document.metadata.get("source_message_ids", [])
    if isinstance(source_message_ids, list):
        values.extend((str(value), "source_message_id") for value in source_message_ids)
    values.extend((value, "member_locator") for value in chunk.member_ids)
    for key in _MODEL_METADATA_KEYS:
        values.extend(
            (value, "model_id") for value in _model_identity_values(document.metadata.get(key))
        )
    for url in _URL.findall(chunk.text):
        values.append((url, "url"))
    for bvid in _BVID.findall(chunk.text):
        values.append((bvid, "bvid"))
    for video_id in _YOUTUBE.findall(chunk.text):
        values.append((video_id, "youtube_id"))
    for pattern in _PRODUCT_MODEL_PATTERNS:
        values.extend((match.group(0), "model_id") for match in pattern.finditer(chunk.text))
    seen: set[tuple[str, str]] = set()
    for raw, kind in values:
        value = canonical_url(raw) if kind == "url" else raw.strip()
        if not value:
            continue
        pair = (value.casefold(), kind)
        if pair not in seen:
            seen.add(pair)
            yield pair


def load_manifest(generation_dir: Path) -> dict[str, Any]:
    generation_dir = _validate_generation_directory(generation_dir)
    path = generation_dir / "manifest.json"
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read generation manifest: {path}: {exc}") from exc
    if not isinstance(manifest, dict):
        raise ValueError("generation manifest must be an object")
    if manifest.get("generation_id") != generation_dir.name:
        raise ValueError("generation directory basename does not match manifest generation_id")
    return manifest


def verify_generation(
    generation_dir: Path,
    *,
    expected: GenerationConfig | None = None,
    full: bool = False,
) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    try:
        generation_dir = _validate_generation_directory(generation_dir)
        manifest = load_manifest(generation_dir)
    except ValueError as exc:
        return {"ok": False, "dense_enabled": False, "errors": [str(exc)], "warnings": []}
    catalog = generation_dir / "catalog.sqlite"
    vectors = generation_dir / "vectors.f32"
    if manifest.get("manifest_hash") != compute_manifest_hash(manifest):
        errors.append("manifest_hash_mismatch")
    if manifest.get("schema_version") != SCHEMA_VERSION:
        errors.append("schema_version_mismatch")
    if manifest.get("status") not in {"ready", "active", "retired"}:
        errors.append("generation_incomplete")
    if expected is not None:
        checks = {
            "model_id": expected.model_id,
            "model_revision": expected.model_revision,
            "reranker_model_id": expected.reranker_model_id,
            "reranker_revision": expected.reranker_revision,
            "dimension": expected.dimension,
            "dtype": expected.dtype,
            "normalized": expected.normalized,
            "query_template": expected.query_template,
            "document_template": expected.document_template,
            "chunker_version": expected.chunker_version,
            "payload_version": expected.payload_version,
            "hard_max_tokens": expected.hard_max_tokens,
        }
        for key, value in checks.items():
            if manifest.get(key) != value:
                errors.append(f"{key}_mismatch")
    dimension = manifest.get("dimension")
    row_count = manifest.get("row_count")
    if not isinstance(dimension, int) or not isinstance(row_count, int):
        errors.append("manifest_shape_invalid")
    elif row_count <= 0:
        errors.append("empty_generation")
    if not catalog.is_file():
        errors.append("catalog_missing")
    if not vectors.is_file():
        errors.append("vectors_missing")
    if catalog.is_file() and manifest.get("catalog_hash") != sha256_file(catalog):
        errors.append("catalog_hash_mismatch")
    if vectors.is_file() and manifest.get("vectors_hash") != sha256_file(vectors):
        errors.append("vectors_hash_mismatch")
    expected_size = (
        row_count * dimension * 4
        if isinstance(row_count, int) and isinstance(dimension, int)
        else -1
    )
    if vectors.is_file() and vectors.stat().st_size != expected_size:
        errors.append("vector_blob_length_mismatch")
    stats: dict[str, Any] = {"row_count": row_count, "dimension": dimension}
    if catalog.is_file():
        try:
            conn = sqlite3.connect(f"file:{catalog}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            foreign_key_errors = list(conn.execute("PRAGMA foreign_key_check"))
            counts = {
                "chunks": conn.execute("SELECT count(*) FROM chunks").fetchone()[0],
                "active_chunks": conn.execute(
                    "SELECT count(*) FROM chunks c JOIN content_items i USING(content_id) "
                    "WHERE c.active=1 AND i.active=1"
                ).fetchone()[0],
                "vectors": conn.execute(
                    "SELECT count(*) FROM vector_rows WHERE generation_id=? AND state='ready'",
                    (manifest.get("generation_id"),),
                ).fetchone()[0],
                "orphan_vectors": conn.execute(
                    "SELECT count(*) FROM vector_rows vr LEFT JOIN chunks c USING(chunk_id) "
                    "WHERE c.chunk_id IS NULL"
                ).fetchone()[0],
                "fts": conn.execute("SELECT count(*) FROM chunk_fts").fetchone()[0],
                "orphan_chunks": conn.execute(
                    "SELECT count(*) FROM chunks c LEFT JOIN content_items i "
                    "USING(content_id) WHERE i.content_id IS NULL"
                ).fetchone()[0],
                "orphan_assets": conn.execute(
                    "SELECT count(*) FROM assets a LEFT JOIN content_items i "
                    "USING(content_id) WHERE i.content_id IS NULL"
                ).fetchone()[0],
                "orphan_fts": conn.execute(
                    "SELECT count(*) FROM chunk_fts f LEFT JOIN chunks c "
                    "ON c.chunk_id=f.chunk_id WHERE c.chunk_id IS NULL"
                ).fetchone()[0],
                "orphan_exact_content": conn.execute(
                    "SELECT count(*) FROM exact_terms e LEFT JOIN content_items i "
                    "ON i.content_id=e.content_id WHERE i.content_id IS NULL"
                ).fetchone()[0],
                "orphan_exact_chunks": conn.execute(
                    "SELECT count(*) FROM exact_terms e LEFT JOIN chunks c "
                    "ON c.chunk_id=e.chunk_id WHERE e.chunk_id IS NOT NULL "
                    "AND c.chunk_id IS NULL"
                ).fetchone()[0],
                "orphan_source_links": conn.execute(
                    "SELECT count(*) FROM source_links s LEFT JOIN content_items i "
                    "USING(content_id) WHERE i.content_id IS NULL"
                ).fetchone()[0],
            }
            # Keep the legacy metric key consumed by release evaluation while
            # exposing the precise error/count name used by catalog guards.
            counts["orphans"] = counts["orphan_vectors"]
            stats.update(counts)
            if integrity != "ok":
                errors.append("catalog_integrity_failed")
            if foreign_key_errors:
                errors.append("catalog_foreign_key_failed")
            if counts["chunks"] != row_count or counts["vectors"] != row_count:
                errors.append("catalog_vector_count_mismatch")
            if counts["fts"] != counts["chunks"]:
                errors.append("fts_chunk_count_mismatch")
            errors.extend(
                name
                for name, value in counts.items()
                if name.startswith("orphan_") and value
            )
            vector_rows = conn.execute(
                "SELECT generation_id,row_index,vector_hash,state FROM vector_rows ORDER BY row_index"
            ).fetchall()
            if any(row["generation_id"] != manifest.get("generation_id") for row in vector_rows):
                errors.append("foreign_generation_vector_rows")
            if [row["row_index"] for row in vector_rows] != list(range(len(vector_rows))):
                errors.append("vector_row_index_not_contiguous")
            if any(row["state"] != "ready" or not row["vector_hash"] for row in vector_rows):
                errors.append("vector_row_not_ready")
            generation_rows = conn.execute("SELECT * FROM embedding_generations").fetchall()
            generation = generation_rows[0] if len(generation_rows) == 1 else None
            if generation is None:
                errors.append("catalog_generation_count_mismatch")
            elif generation["manifest_hash"] != manifest.get("manifest_hash"):
                errors.append("catalog_manifest_mismatch")
            else:
                catalog_expected = {
                    "generation_id": manifest.get("generation_id"),
                    "baseline_generation_id": manifest.get("baseline_generation_id"),
                    "baseline_pointer": manifest.get("baseline_pointer"),
                    "model_id": manifest.get("model_id"),
                    "model_revision": manifest.get("model_revision"),
                    "reranker_model_id": manifest.get("reranker_model_id"),
                    "reranker_revision": manifest.get("reranker_revision"),
                    "dimension": manifest.get("dimension"),
                    "normalized": int(bool(manifest.get("normalized"))),
                    "query_template": manifest.get("query_template"),
                    "document_template": manifest.get("document_template"),
                    "chunker_version": manifest.get("chunker_version"),
                    "payload_version": manifest.get("payload_version"),
                }
                if any(generation[key] != value for key, value in catalog_expected.items()):
                    errors.append("catalog_generation_context_mismatch")
                if generation["status"] not in {"ready", "active", "retired"}:
                    errors.append("catalog_generation_incomplete")
            conn.close()
        except (sqlite3.Error, KeyError, IndexError) as exc:
            errors.append(f"catalog_read_error:{type(exc).__name__}")
    if full and not errors and row_count:
        matrix = np.memmap(vectors, dtype="<f4", mode="r", shape=(row_count, dimension))
        finite = bool(np.isfinite(matrix).all())
        norms = np.linalg.norm(matrix, axis=1)
        stats.update(
            {
                "finite": finite,
                "norm_min": float(norms.min()),
                "norm_max": float(norms.max()),
                "coverage": 1.0,
            }
        )
        if not finite:
            errors.append("nonfinite_vectors")
        if (
            abs(float(norms.min()) - 1.0) > UNIT_NORM_TOLERANCE
            or abs(float(norms.max()) - 1.0) > UNIT_NORM_TOLERANCE
        ):
            errors.append("vector_norm_out_of_range")
        conn = sqlite3.connect(f"file:{catalog}?mode=ro", uri=True)
        hashes = {
            int(row[0]): row[1]
            for row in conn.execute("SELECT row_index,vector_hash FROM vector_rows")
        }
        conn.close()
        for index in range(row_count):
            raw = np.asarray(matrix[index], dtype="<f4").tobytes(order="C")
            if hashes.get(index) != sha256_bytes(raw):
                errors.append("vector_row_hash_mismatch")
                break
    return {
        "ok": not errors,
        "dense_enabled": not errors,
        "generation_id": manifest.get("generation_id"),
        "status": manifest.get("status"),
        "manifest": manifest,
        "stats": stats,
        "errors": errors,
        "warnings": warnings,
    }


def _stat_identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    """Return fields that expose replacement or in-place mutation of a file."""

    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _hash_online_artifact(
    path: Path,
    *,
    sealed_hash: Any,
    label: str,
) -> tuple[int | None, os.stat_result | None, list[str]]:
    """Hash one regular artifact while proving it was not replaced in-flight."""

    errors: list[str] = []
    size: int | None = None
    after_hash: os.stat_result | None = None
    try:
        before_hash = os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        errors.append(f"{label}_missing")
        return size, after_hash, errors
    except OSError as exc:
        errors.append(f"{label}_stat_error:{type(exc).__name__}")
        return size, after_hash, errors
    if not stat.S_ISREG(before_hash.st_mode) or path.is_symlink():
        errors.append(f"{label}_unsafe")
        return size, after_hash, errors
    size = before_hash.st_size
    try:
        actual_hash = sha256_file(path)
        after_hash = os.stat(path, follow_symlinks=False)
    except Exception as exc:
        errors.append(f"{label}_hash_error:{type(exc).__name__}")
        return size, after_hash, errors
    if _stat_identity(before_hash) != _stat_identity(after_hash):
        errors.append(f"{label}_changed_during_verification")
    if actual_hash != sealed_hash:
        errors.append(f"{label}_hash_mismatch")
    return size, after_hash, errors


def _open_online_verified_generation(
    generation_dir: Path,
    *,
    expected: GenerationConfig | None = None,
) -> tuple[dict[str, Any], dict[str, Any], sqlite3.Connection | None]:
    """Verify sealed online artifacts without running offline database audits.

    Activation and :func:`verify_generation` retain integrity, foreign-key,
    orphan and per-vector checks.  Query workers instead prove the immutable
    catalog/vector seals, generation context, retrieval-table cardinalities and
    confirmed source-link shape.  This keeps reader construction bounded while
    still rejecting an unsealed or structurally incompatible catalog.
    """

    generation_dir = _validate_generation_directory(generation_dir)
    manifest = load_manifest(generation_dir)
    errors: list[str] = []
    warnings: list[str] = []
    if manifest.get("manifest_hash") != compute_manifest_hash(manifest):
        errors.append("manifest_hash_mismatch")
    if manifest.get("schema_version") != SCHEMA_VERSION:
        errors.append("schema_version_mismatch")
    if manifest.get("status") not in {"ready", "active", "retired"}:
        errors.append("generation_incomplete")

    dimension = manifest.get("dimension")
    row_count = manifest.get("row_count")
    if type(dimension) is not int or type(row_count) is not int:
        errors.append("manifest_shape_invalid")
        expected_vector_size: int | None = None
    elif dimension <= 0 or row_count <= 0:
        errors.append("empty_generation")
        expected_vector_size = None
    else:
        expected_vector_size = row_count * dimension * 4
    required_context = (
        "model_id",
        "model_revision",
        "reranker_model_id",
        "reranker_revision",
        "dtype",
        "normalized",
        "query_template",
        "document_template",
        "chunker_version",
        "payload_version",
        "hard_max_tokens",
    )
    if any(field not in manifest for field in required_context):
        errors.append("manifest_shape_invalid")
    if expected is not None:
        for key, value in {
            "model_id": expected.model_id,
            "model_revision": expected.model_revision,
            "reranker_model_id": expected.reranker_model_id,
            "reranker_revision": expected.reranker_revision,
            "dimension": expected.dimension,
            "dtype": expected.dtype,
            "normalized": expected.normalized,
            "query_template": expected.query_template,
            "document_template": expected.document_template,
            "chunker_version": expected.chunker_version,
            "payload_version": expected.payload_version,
            "hard_max_tokens": expected.hard_max_tokens,
        }.items():
            if manifest.get(key) != value:
                errors.append(f"{key}_mismatch")

    catalog_path = generation_dir / "catalog.sqlite"
    sealed_catalog_hash = manifest.get("catalog_hash")
    if re.fullmatch(r"[0-9a-f]{64}", str(sealed_catalog_hash or "")) is None:
        errors.append("catalog_hash_invalid")
    vectors_path = generation_dir / "vectors.f32"
    sealed_vectors_hash = manifest.get("vectors_hash")
    if re.fullmatch(r"[0-9a-f]{64}", str(sealed_vectors_hash or "")) is None:
        errors.append("vectors_hash_invalid")
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="knowledge-seal") as pool:
        catalog_future = pool.submit(
            _hash_online_artifact,
            catalog_path,
            sealed_hash=sealed_catalog_hash,
            label="catalog",
        )
        vectors_future = pool.submit(
            _hash_online_artifact,
            vectors_path,
            sealed_hash=sealed_vectors_hash,
            label="vectors",
        )
        try:
            _catalog_size, catalog_after_hash, catalog_errors = catalog_future.result()
        except Exception as exc:
            _catalog_size, catalog_after_hash = None, None
            catalog_errors = [f"catalog_hash_error:{type(exc).__name__}"]
        try:
            vector_size, _vectors_after_hash, vector_errors = vectors_future.result()
        except Exception as exc:
            vector_size, _vectors_after_hash = None, None
            vector_errors = [f"vectors_hash_error:{type(exc).__name__}"]
    errors.extend(catalog_errors)
    errors.extend(vector_errors)
    if expected_vector_size is not None and vector_size != expected_vector_size:
        errors.append("vector_blob_length_mismatch")

    connection: sqlite3.Connection | None = None
    counts: dict[str, Any] = {}
    catalog_seal_errors = {
        "catalog_hash_invalid",
        "catalog_missing",
        "catalog_unsafe",
        "catalog_hash_mismatch",
        "catalog_changed_during_verification",
    }
    catalog_seal_failed = bool(catalog_seal_errors.intersection(errors)) or any(
        value.startswith(("catalog_stat_error:", "catalog_hash_error:"))
        for value in errors
    )
    if not catalog_seal_failed:
        try:
            connection = sqlite3.connect(f"file:{catalog_path}?mode=ro", uri=True)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            generation_rows = connection.execute(
                "SELECT * FROM embedding_generations"
            ).fetchall()
            vector_counts = connection.execute(
                "SELECT count(*) AS total_rows,"
                "coalesce(sum(CASE WHEN generation_id=? AND state='ready' THEN 1 ELSE 0 END),0) "
                "AS ready_rows,min(row_index) AS min_row,max(row_index) AS max_row,"
                "coalesce(sum(CASE WHEN vector_hash IS NULL OR vector_hash='' "
                "OR state!='ready' THEN 1 ELSE 0 END),0) AS invalid_rows "
                "FROM vector_rows",
                (manifest.get("generation_id"),),
            ).fetchone()
            source_counts = connection.execute(
                "SELECT count(*) AS source_links,"
                "coalesce(sum(CASE WHEN confirmed=1 THEN 1 ELSE 0 END),0) "
                "AS confirmed_source_links,"
                "coalesce(sum(CASE WHEN confirmed=1 AND "
                "(trim(ledger_schema)='' OR typeof(chat_id)!='integer' "
                "OR typeof(message_id)!='integer') THEN 1 ELSE 0 END),0) "
                "AS invalid_confirmed_source_links FROM source_links"
            ).fetchone()
            confirmed_linked_source_links = int(
                connection.execute(
                    "SELECT count(*) FROM source_links s JOIN content_items i "
                    "USING(content_id) WHERE s.confirmed=1"
                ).fetchone()[0]
            )
            counts = {
                "content_items": int(
                    connection.execute("SELECT count(*) FROM content_items").fetchone()[0]
                ),
                "chunks": int(connection.execute("SELECT count(*) FROM chunks").fetchone()[0]),
                "fts": int(connection.execute("SELECT count(*) FROM chunk_fts").fetchone()[0]),
                "generation_rows": len(generation_rows),
                "vector_rows": int(vector_counts["total_rows"]),
                "ready_vector_rows": int(vector_counts["ready_rows"]),
                "vector_min_row": vector_counts["min_row"],
                "vector_max_row": vector_counts["max_row"],
                "invalid_vector_rows": int(vector_counts["invalid_rows"]),
                "source_links": int(source_counts["source_links"]),
                "confirmed_source_links": int(source_counts["confirmed_source_links"]),
                "confirmed_linked_source_links": confirmed_linked_source_links,
                "invalid_confirmed_source_links": int(
                    source_counts["invalid_confirmed_source_links"]
                ),
            }
            if counts["content_items"] <= 0 or counts["chunks"] <= 0:
                errors.append("catalog_empty")
            if counts["chunks"] != row_count or counts["vector_rows"] != row_count:
                errors.append("catalog_vector_count_mismatch")
            if counts["ready_vector_rows"] != row_count:
                errors.append("catalog_generation_count_mismatch")
            if counts["fts"] != counts["chunks"]:
                errors.append("fts_chunk_count_mismatch")
            if row_count and (
                counts["vector_min_row"] != 0
                or counts["vector_max_row"] != row_count - 1
                or counts["invalid_vector_rows"]
            ):
                errors.append("vector_row_index_not_contiguous")
            if (
                counts["invalid_confirmed_source_links"]
                or counts["confirmed_linked_source_links"]
                != counts["confirmed_source_links"]
            ):
                errors.append("confirmed_source_link_provenance_invalid")
            if len(generation_rows) != 1:
                errors.append("catalog_generation_context_mismatch")
            else:
                generation = generation_rows[0]
                catalog_expected = {
                    "generation_id": manifest.get("generation_id"),
                    "baseline_generation_id": manifest.get("baseline_generation_id"),
                    "baseline_pointer": manifest.get("baseline_pointer"),
                    "model_id": manifest.get("model_id"),
                    "model_revision": manifest.get("model_revision"),
                    "reranker_model_id": manifest.get("reranker_model_id"),
                    "reranker_revision": manifest.get("reranker_revision"),
                    "dimension": manifest.get("dimension"),
                    "normalized": int(bool(manifest.get("normalized"))),
                    "query_template": manifest.get("query_template"),
                    "document_template": manifest.get("document_template"),
                    "chunker_version": manifest.get("chunker_version"),
                    "payload_version": manifest.get("payload_version"),
                    "manifest_hash": manifest.get("manifest_hash"),
                }
                if any(generation[key] != value for key, value in catalog_expected.items()):
                    errors.append("catalog_generation_context_mismatch")
                if generation["status"] not in {"ready", "active", "retired"}:
                    errors.append("catalog_generation_incomplete")
            try:
                catalog_after_checks = os.stat(catalog_path, follow_symlinks=False)
            except OSError:
                errors.append("catalog_changed_during_verification")
            else:
                if catalog_after_hash is None or _stat_identity(
                    catalog_after_hash
                ) != _stat_identity(catalog_after_checks):
                    errors.append("catalog_changed_during_verification")
        except (sqlite3.Error, KeyError, IndexError, TypeError) as exc:
            if connection is not None:
                connection.close()
                connection = None
            errors.append(f"catalog_read_error:{type(exc).__name__}")

    unique_errors = list(dict.fromkeys(errors))
    stats: dict[str, Any] = {
        "row_count": row_count,
        "dimension": dimension,
        "vector_size": vector_size,
        **counts,
    }
    return (
        manifest,
        {
            "ok": not unique_errors,
            "dense_enabled": not unique_errors,
            "generation_id": manifest.get("generation_id"),
            "status": manifest.get("status"),
            "manifest": manifest,
            "stats": stats,
            "errors": unique_errors,
            "warnings": warnings,
        },
        connection,
    )


def retrieval_scope_sql(scope):
    """Bind content/time restrictions before each retrieval channel's top-k."""
    if scope is None:return '', []
    if not isinstance(scope,dict) or set(scope)-{'content_ids','published_after','published_before'}:
        raise ValueError('invalid retrieval scope')
    clauses=[];params=[]
    if 'content_ids' in scope:
        ids=scope['content_ids']
        if not isinstance(ids,list) or any(not isinstance(v,str) or not v for v in ids):
            raise ValueError('invalid content scope identities')
        clauses.append('i.content_id IN (SELECT value FROM json_each(?))')
        params.append(json.dumps(sorted(set(ids))))
    for field,operator in [('published_after','>='),('published_before','<=')]:
        if field in scope:
            value=scope[field]
            parsed=datetime.fromisoformat(value.replace('Z','+00:00'))
            if parsed.tzinfo is None:raise ValueError('scope dates require timezone')
            clauses.append('julianday(i.published_at) '+operator+' julianday(?)')
            params.append(value)
    return (' AND '+' AND '.join(clauses) if clauses else ''), params


class GenerationReader:
    def __init__(
        self,
        generation_dir: Path,
        client: QwenRuntimeClient,
        counter: TokenCounter,
        *,
        expected: GenerationConfig | None = None,
        _online_state: tuple[
            dict[str, Any], dict[str, Any], sqlite3.Connection | None
        ]
        | None = None,
    ):
        self.generation_dir = _validate_generation_directory(generation_dir)
        self.client = client
        self.counter = counter
        self.catalog_path = self.generation_dir / "catalog.sqlite"
        if _online_state is None:
            self.manifest, self.verification, connection = _open_online_verified_generation(
                self.generation_dir, expected=expected
            )
        else:
            self.manifest, self.verification, connection = _online_state
            if self.manifest.get("generation_id") != self.generation_dir.name:
                if connection is not None:
                    connection.close()
                raise ValueError("online reader state generation mismatch")
        catalog_unsafe = {
            "catalog_missing",
            "catalog_unsafe",
            "catalog_hash_invalid",
            "catalog_hash_mismatch",
            "fts_chunk_count_mismatch",
            "catalog_vector_count_mismatch",
            "catalog_generation_count_mismatch",
            "catalog_manifest_mismatch",
            "catalog_generation_context_mismatch",
            "catalog_generation_incomplete",
            "catalog_empty",
            "vector_row_index_not_contiguous",
            "confirmed_source_link_provenance_invalid",
            "catalog_changed_during_verification",
        }
        unsafe_errors = [
            str(value)
            for value in self.verification.get("errors", [])
            if str(value) in catalog_unsafe
            or str(value).startswith(("catalog_read_error:", "catalog_hash_error:"))
        ]
        if unsafe_errors:
            if connection is not None:
                connection.close()
            raise ValueError(
                "generation catalog failed trusted-reader checks: "
                + ",".join(unsafe_errors)
            )
        if connection is None:
            raise ValueError("generation catalog is unavailable to the trusted reader")
        self.conn = connection
        self.dense_enabled = bool(self.verification["dense_enabled"])
        self.degraded_reasons = list(self.verification["errors"])
        runtime_mismatches = []
        if client.embedding_model != self.manifest.get("model_id"):
            runtime_mismatches.append("query_model_mismatch")
        if str(getattr(client, "embedding_revision", "")) != self.manifest.get(
            "model_revision"
        ):
            runtime_mismatches.append("query_model_revision_mismatch")
        if client.reranker_model != self.manifest.get("reranker_model_id"):
            runtime_mismatches.append("reranker_model_mismatch")
        client_reranker_revision = str(getattr(client, "reranker_revision", ""))
        if client_reranker_revision and client_reranker_revision != self.manifest.get(
            "reranker_revision"
        ):
            runtime_mismatches.append("reranker_revision_mismatch")
        if client.dimension != self.manifest.get("dimension"):
            runtime_mismatches.append("query_dimension_mismatch")
        if runtime_mismatches:
            self.dense_enabled = False
            self.degraded_reasons.extend(runtime_mismatches)
        if self.dense_enabled:
            self.matrix = np.memmap(
                self.generation_dir / "vectors.f32",
                dtype="<f4",
                mode="r",
                shape=(self.manifest["row_count"], self.manifest["dimension"]),
            )
        else:
            self.matrix = None
            log.warning(
                "knowledge dense generation disabled: %s",
                canonical_json(
                    {
                        "generation": self.manifest.get("generation_id"),
                        "errors": self.degraded_reasons,
                    }
                ),
            )

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()
            self.conn = None

    def search(
        self,
        query: str,
        *,
        top_k: int = 8,
        exact_limit: int = 40,
        lexical_limit: int = 40,
        dense_limit: int = 40,
        rerank_limit: int = 30,
        use_reranker: bool = True,
        use_dense: bool = True,
        timeout: float = ONLINE_QUERY_TIMEOUT,
        precomputed_query: PrecomputedQueryEmbedding | None = None,
        content_scope: dict | None = None,
    ) -> dict[str, Any]:
        if timeout <= 0:
            raise ValueError("online query timeout must be positive")
        started = time.perf_counter()
        deadline = time.monotonic() + min(ONLINE_QUERY_TIMEOUT, timeout)
        timings = {
            "exact": 0.0,
            "lexical": 0.0,
            "query_embedding": 0.0,
            "dense_lookup": 0.0,
            "rerank": 0.0,
        }
        query_degraded = list(self.degraded_reasons)
        precomputed_failure_type: str | None = None

        def remaining() -> float:
            value = deadline - time.monotonic()
            if value <= 0:
                raise TimeoutError("online query exceeded 8-second deadline")
            return value

        clean_query = query.strip()
        if not clean_query:
            raise ValueError("query must not be empty")

        scope_sql,scope_params=retrieval_scope_sql(content_scope)
        scope_kwargs={"scope":content_scope} if content_scope is not None else {}
        phase_started = time.perf_counter()
        exact = self._exact(clean_query, exact_limit, **scope_kwargs)
        timings["exact"] = (time.perf_counter() - phase_started) * 1000
        try:
            remaining()
            phase_started = time.perf_counter()
            lexical = self._lexical(clean_query, lexical_limit, **scope_kwargs)
            timings["lexical"] = (time.perf_counter() - phase_started) * 1000
        except TimeoutError:
            lexical = []
            query_degraded.append("online_deadline_exceeded")

        dense: list[str] = []
        if use_dense and self.dense_enabled and "online_deadline_exceeded" not in query_degraded:
            try:
                rendered_query = render_query(clean_query, template=self.manifest["query_template"])
                if self.counter.count(rendered_query) > self.manifest["hard_max_tokens"]:
                    raise ValueError("query exceeds generation hard token max")
                if precomputed_query is None:
                    phase_started = time.perf_counter()
                    query_vector = self.client.embed_queries(
                        [rendered_query], timeout=remaining()
                    )[0]
                    query_vector = _normalize_vector(
                        query_vector, self.manifest["dimension"]
                    )
                    timings["query_embedding"] = (
                        time.perf_counter() - phase_started
                    ) * 1000
                else:
                    expected_precomputed = {
                        "query": clean_query,
                        "rendered_query": rendered_query,
                        "generation_id": self.manifest["generation_id"],
                        "manifest_hash": self.manifest["manifest_hash"],
                        "model_id": self.manifest["model_id"],
                        "model_revision": self.manifest["model_revision"],
                        "dimension": self.manifest["dimension"],
                        "query_template": self.manifest["query_template"],
                    }
                    if any(
                        getattr(precomputed_query, key) != value
                        for key, value in expected_precomputed.items()
                    ):
                        raise QwenRuntimeError(
                            "precomputed query embedding context mismatch"
                        )
                    elapsed_ms = float(precomputed_query.elapsed_ms)
                    if not math.isfinite(elapsed_ms) or elapsed_ms < 0:
                        raise QwenRuntimeError(
                            "precomputed query embedding timing is invalid"
                        )
                    timings["query_embedding"] = elapsed_ms
                    if precomputed_query.error_type is not None:
                        if re.fullmatch(
                            r"[A-Za-z][A-Za-z0-9_]*",
                            precomputed_query.error_type,
                        ) is None:
                            raise QwenRuntimeError(
                                "precomputed query embedding error type is invalid"
                            )
                        precomputed_failure_type = precomputed_query.error_type
                        raise QwenRuntimeError(
                            "precomputed query embedding failed: "
                            + precomputed_query.error_type
                        )
                    if precomputed_query.vector is None:
                        raise QwenRuntimeError(
                            "precomputed query embedding vector is missing"
                        )
                    query_vector = _normalize_vector(
                        precomputed_query.vector, self.manifest["dimension"]
                    )

                remaining()
                phase_started = time.perf_counter()
                active_rows = self.conn.execute(
                    "SELECT vr.row_index,vr.chunk_id FROM vector_rows vr "
                    "JOIN chunks c USING(chunk_id) JOIN content_items i USING(content_id) "
                    "WHERE vr.generation_id=? AND vr.state='ready' "
                    "AND c.active=1 AND i.active=1 " + scope_sql + " ORDER BY vr.row_index",
                    [self.manifest["generation_id"], *scope_params],
                ).fetchall()
                active_indices = np.asarray(
                    [int(row["row_index"]) for row in active_rows], dtype=np.int64
                )
                if len(active_indices) == self.manifest["row_count"] and np.array_equal(
                    active_indices, np.arange(len(active_indices), dtype=np.int64)
                ):
                    # Avoid a full fancy-index copy (hundreds of MB at 4096d)
                    # when every vector row is active and already contiguous.
                    scores = np.asarray(self.matrix @ query_vector, dtype=np.float32)
                elif len(active_indices):
                    scores = np.asarray(
                        self.matrix[active_indices] @ query_vector, dtype=np.float32
                    )
                else:
                    scores = np.empty((0,), dtype=np.float32)
                take = min(dense_limit, len(scores))
                if take:
                    ids = np.argpartition(-scores, take - 1)[:take]
                    ids = ids[np.argsort(-scores[ids])]
                    dense = [active_rows[int(value)]["chunk_id"] for value in ids]
                timings["dense_lookup"] = (time.perf_counter() - phase_started) * 1000
                remaining()
            except Exception as exc:
                query_degraded.append(
                    "dense_query_failed:"
                    + (precomputed_failure_type or type(exc).__name__)
                )
                log.warning("knowledge dense query failed; falling back to lexical: %s", exc)

        ranked = rrf_merge(
            {"exact": exact, "fts": lexical, "dense": dense},
            self.conn,
            union_cap=60,
            chunks_per_content=3,
        )
        rerank_failed = False
        rerank_scores: dict[str, float] = {}
        if (
            use_dense
            and self.dense_enabled
            and use_reranker
            and ranked
            and "online_deadline_exceeded" not in query_degraded
            and not any(
                reason.startswith("dense_query_failed:")
                for reason in query_degraded
            )
        ):
            candidates = ranked[: min(rerank_limit, len(ranked))]
            try:
                rows = self._rows([chunk_id for chunk_id, _, _ in candidates])
                payloads = [rows[chunk_id]["rendered_text"] for chunk_id, _, _ in candidates]
                mapping = [chunk_id for chunk_id, _, _ in candidates]
                query_tokens = self.counter.count(clean_query)
                payload_token_counts = [self.counter.count(payload) for payload in payloads]
                if any(
                    query_tokens + payload_tokens + RERANK_TOKEN_RESERVE > RERANK_MAX_TOKENS
                    for payload_tokens in payload_token_counts
                ):
                    raise ValueError("reranker query/document token budget exceeds runtime max")
                phase_started = time.perf_counter()
                results = self.client.rerank(
                    clean_query,
                    payloads,
                    top_n=len(payloads),
                    timeout=remaining(),
                    document_token_counts=payload_token_counts,
                )
                timings["rerank"] = (time.perf_counter() - phase_started) * 1000
                if len(results) != len(payloads):
                    raise QwenRuntimeError("reranker returned a partial candidate list")
                reranked = []
                by_chunk = {chunk_id: (score, channels) for chunk_id, score, channels in candidates}
                for item in results:
                    chunk_id = mapping[item["index"]]
                    rerank_scores[chunk_id] = item["relevance_score"]
                    rrf_score, channels = by_chunk[chunk_id]
                    reranked.append((chunk_id, rrf_score, channels))
                ranked = reranked + [item for item in ranked if item[0] not in rerank_scores]
            except Exception as exc:
                rerank_failed = True
                query_degraded.append(f"rerank_failed:{type(exc).__name__}")
                log.warning("knowledge reranker failed; preserving RRF order: %s", exc)

        ranked = _pin_exact_hits(ranked, exact)

        # Resolve the de-duplicated content rows first, then fetch all confirmed
        # source links in one query.  The previous loop issued one SQLite query
        # per result (N+1 at ``top_k``), even though links are immutable for the
        # sealed generation and share the same content-id key.
        selected: list[tuple[str, float, tuple[str, ...], sqlite3.Row]] = []
        seen_content: set[str] = set()
        rows = self._rows([chunk_id for chunk_id, _, _ in ranked])
        for chunk_id, rrf_score, channels in ranked:
            row = rows.get(chunk_id)
            if row is None or row["content_id"] in seen_content:
                continue
            seen_content.add(row["content_id"])
            selected.append((chunk_id, rrf_score, channels, row))
            if len(selected) >= top_k:
                break

        links_by_content: dict[str, tuple[dict, ...]] = {}
        content_ids = [row["content_id"] for _, _, _, row in selected]
        if content_ids:
            placeholders = ",".join("?" for _ in content_ids)
            link_rows = self.conn.execute(
                "SELECT content_id,chat_id,thread_id,message_id,source_message_id,"
                "ledger_schema,confirmed FROM source_links "
                f"WHERE content_id IN ({placeholders}) AND confirmed=1 "
                "ORDER BY content_id,message_id",
                content_ids,
            ).fetchall()
            grouped: dict[str, list[dict]] = {}
            for link in link_rows:
                values = dict(link)
                content_id = values.pop("content_id")
                grouped.setdefault(content_id, []).append(values)
            links_by_content = {
                content_id: tuple(values) for content_id, values in grouped.items()
            }

        final: list[SearchHit] = []
        for chunk_id, rrf_score, channels, row in selected:
            try:
                raw_member_ids = json.loads(row["member_ids_json"])
            except (json.JSONDecodeError, TypeError) as exc:
                raise ValueError("chunk member_ids_json is invalid") from exc
            if not isinstance(raw_member_ids, list) or any(
                not isinstance(value, str) or not value.strip() for value in raw_member_ids
            ):
                raise ValueError("chunk member_ids_json must contain stable string identities")
            final.append(
                SearchHit(
                    rank=len(final) + 1,
                    content_id=row["content_id"],
                    chunk_id=chunk_id,
                    title=row["title"],
                    source_kind=row["source_kind"],
                    source_ref=row["source_ref"],
                    canonical_url=row["canonical_url"],
                    published_at=row["published_at"],
                    authority=row["authority"],
                    mapping_status=row["mapping_status"],
                    text=row["text"],
                    locator=row["locator"],
                    rrf_score=rrf_score,
                    rerank_score=rerank_scores.get(chunk_id),
                    channels=tuple(channels),
                    member_ids=tuple(raw_member_ids),
                    source_links=links_by_content.get(row["content_id"], ()),
                )
            )
        elapsed = (time.perf_counter() - started) * 1000
        timings["lookup"] = timings["exact"] + timings["lexical"] + timings["dense_lookup"]
        # A prepared embedding completed before ``search`` started, but it is
        # still part of the online query's end-to-end work just as it was when
        # performed inline.  Preserve the existing timing contract rather than
        # reporting an artificial latency improvement.
        timings["total"] = elapsed + (
            timings["query_embedding"] if precomputed_query is not None else 0.0
        )
        return {
            "generation_id": self.manifest["generation_id"],
            "dense_enabled": self.dense_enabled,
            "reranker_used": use_reranker and not rerank_failed and bool(rerank_scores),
            "degraded": bool(query_degraded),
            "degraded_reasons": list(dict.fromkeys(query_degraded)),
            "timing_ms": {key: round(value, 2) for key, value in timings.items()},
            "hits": [asdict(hit) for hit in final],
        }

    def _exact(self, query: str, limit: int, *, scope=None) -> list[str]:
        scope_sql,scope_params=retrieval_scope_sql(scope)
        terms = {query.casefold(), canonical_url(query).casefold()}
        terms.update(canonical_url(value).casefold() for value in _URL.findall(query))
        terms.update(value.casefold() for value in _BVID.findall(query))
        terms.update(value.casefold() for value in _YOUTUBE.findall(query))
        terms.update(value.casefold() for value in re.findall(r"[\w:-]{6,}", query))
        terms.discard("")
        if not terms:
            return []
        placeholders = ",".join("?" for _ in terms)
        rows = self.conn.execute(
            f"SELECT e.chunk_id FROM exact_terms e JOIN chunks c ON c.chunk_id=e.chunk_id "
            f"JOIN content_items i ON i.content_id=e.content_id "
            f"WHERE e.term IN ({placeholders}) AND c.active=1 AND i.active=1 "
            + scope_sql + " ORDER BY CASE WHEN e.kind='content_id' THEN 0 WHEN e.kind='url' THEN 1 "
            "WHEN e.kind LIKE '%confirmed' THEN 2 ELSE 3 END LIMIT ?",
            [*sorted(terms), *scope_params, limit],
        ).fetchall()
        return list(dict.fromkeys(row["chunk_id"] for row in rows if row["chunk_id"]))

    def _lexical(self, query: str, limit: int, *, scope=None) -> list[str]:
        scope_sql,scope_params=retrieval_scope_sql(scope)
        phrase = '"' + query.replace('"', '""') + '"'
        try:
            rows = self.conn.execute(
                "SELECT f.chunk_id FROM chunk_fts f JOIN chunks c ON c.chunk_id=f.chunk_id "
                "JOIN content_items i USING(content_id) WHERE chunk_fts MATCH ? "
                "AND c.active=1 AND i.active=1 " + scope_sql + " ORDER BY bm25(chunk_fts) LIMIT ?",
                [phrase, *scope_params, limit],
            ).fetchall()
        except sqlite3.OperationalError:
            parts = [part for part in re.split(r"\s+", query) if len(part) >= 2]
            if not parts:
                return []
            expression = " OR ".join('"' + part.replace('"', '""') + '"' for part in parts)
            rows = self.conn.execute(
                "SELECT f.chunk_id FROM chunk_fts f JOIN chunks c ON c.chunk_id=f.chunk_id "
                "JOIN content_items i USING(content_id) WHERE chunk_fts MATCH ? "
                "AND c.active=1 AND i.active=1 " + scope_sql + " ORDER BY bm25(chunk_fts) LIMIT ?",
                [expression, *scope_params, limit],
            ).fetchall()
        return [row["chunk_id"] for row in rows]

    def _rows(self, chunk_ids: Sequence[str]) -> dict[str, sqlite3.Row]:
        if not chunk_ids:
            return {}
        placeholders = ",".join("?" for _ in chunk_ids)
        rows = self.conn.execute(
            f"SELECT c.*,i.title,i.source_kind,i.source_ref,i.canonical_url,i.published_at,"
            f"i.authority,i.mapping_status "
            f"FROM chunks c JOIN content_items i USING(content_id) "
            f"WHERE c.chunk_id IN ({placeholders}) AND c.active=1 AND i.active=1",
            list(chunk_ids),
        ).fetchall()
        return {row["chunk_id"]: row for row in rows}


class LexicalGenerationReader(GenerationReader):
    """Read exact/FTS results only from one independently sealed catalog.

    This reader deliberately does not construct a tokenizer or Qwen client.  It
    exists for the fail-open path where the dense generation context is
    incompatible or incomplete but the immutable catalog can still be proven
    to be the catalog named by the parseable manifest.  An unreadable manifest,
    a catalog hash mismatch, or any catalog integrity/provenance failure is not
    a lexical fallback: the generation is rejected so it cannot return
    untrusted source links as if they were valid results.
    """

    def __init__(
        self,
        generation_dir: Path,
        *,
        degraded_reasons: Sequence[str] = (),
    ) -> None:
        self.generation_dir = _validate_generation_directory(generation_dir)
        self.client = None
        self.counter = None
        self.catalog_path = self.generation_dir / "catalog.sqlite"
        self.manifest, verification, connection = _open_online_verified_generation(
            self.generation_dir
        )
        lexical_unsafe = {
            "catalog_missing",
            "catalog_unsafe",
            "catalog_hash_invalid",
            "catalog_hash_mismatch",
            "fts_chunk_count_mismatch",
            "catalog_vector_count_mismatch",
            "catalog_generation_count_mismatch",
            "catalog_generation_incomplete",
            "catalog_empty",
            "vector_row_index_not_contiguous",
            "confirmed_source_link_provenance_invalid",
            "catalog_changed_during_verification",
        }
        unsafe_errors = [
            str(value)
            for value in verification.get("errors", [])
            if str(value) in lexical_unsafe
            or str(value).startswith(("catalog_read_error:", "catalog_hash_error:"))
        ]
        if unsafe_errors:
            if connection is not None:
                connection.close()
            if "catalog_hash_mismatch" in unsafe_errors:
                raise ValueError("generation catalog seal mismatch; lexical fallback is unsafe")
            raise ValueError(
                "generation catalog failed lexical safety checks: "
                + ",".join(unsafe_errors)
            )
        if connection is None:
            raise ValueError("generation catalog is unavailable for lexical fallback")
        self.conn = connection
        self.verification = verification
        self.dense_enabled = False
        self.matrix = None
        reasons = [
            *verification.get("errors", []),
            *degraded_reasons,
            "lexical_only",
        ]
        self.degraded_reasons = list(dict.fromkeys(str(value) for value in reasons if value))

def rrf_merge(
    channels: dict[str, Sequence[str]],
    conn: sqlite3.Connection,
    *,
    union_cap: int,
    chunks_per_content: int,
) -> list[tuple[str, float, tuple[str, ...]]]:
    scores: dict[str, float] = {}
    membership: dict[str, set[str]] = {}
    for channel, values in channels.items():
        for rank, chunk_id in enumerate(values, 1):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (RRF_K + rank)
            membership.setdefault(chunk_id, set()).add(channel)
    if not scores:
        return []
    ids = list(scores)
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT c.chunk_id,c.content_id FROM chunks c JOIN content_items i USING(content_id) "
        f"WHERE c.chunk_id IN ({placeholders}) AND c.active=1 AND i.active=1",
        ids,
    ).fetchall()
    content_by_chunk = {row["chunk_id"]: row["content_id"] for row in rows}
    counts: dict[str, int] = {}
    output: list[tuple[str, float, tuple[str, ...]]] = []
    for chunk_id in sorted(scores, key=lambda value: scores[value], reverse=True):
        content_id = content_by_chunk.get(chunk_id)
        if content_id is None or counts.get(content_id, 0) >= chunks_per_content:
            continue
        counts[content_id] = counts.get(content_id, 0) + 1
        output.append((chunk_id, scores[chunk_id], tuple(sorted(membership[chunk_id]))))
        if len(output) >= union_cap:
            break
    return output


def _pin_exact_hits(
    ranked: Sequence[tuple[str, float, tuple[str, ...]]], exact: Sequence[str]
) -> list[tuple[str, float, tuple[str, ...]]]:
    """Keep exact recall deterministic even when RRF/reranking favors semantics."""
    by_chunk = {item[0]: item for item in ranked}
    pinned = [by_chunk[chunk_id] for chunk_id in exact if chunk_id in by_chunk]
    pinned_ids = {item[0] for item in pinned}
    return pinned + [item for item in ranked if item[0] not in pinned_ids]


def read_raw_pointer(root: Path, name: str) -> str:
    """Read and validate one pointer without requiring its target to exist."""

    if name not in {"CURRENT", "PREVIOUS"}:
        raise ValueError(f"invalid knowledge pointer name: {name!r}")
    root = root.expanduser()
    path = root / name
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"knowledge pointer must be a regular non-symlink file: {path}")
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ValueError(f"cannot read knowledge pointer: {path}") from exc
    try:
        _validate_generation_id(value)
    except ValueError as exc:
        raise ValueError(f"invalid knowledge pointer {name}: {value!r}") from exc
    return value


def read_pointer(root: Path, name: str) -> str:
    value = read_raw_pointer(root, name)
    root = root.expanduser()
    try:
        generation_dir = _validate_generation_directory(root / "generations" / value)
    except ValueError as exc:
        raise ValueError(f"invalid knowledge pointer {name}: {value!r}") from exc
    if not generation_dir.is_dir():
        raise ValueError(f"knowledge pointer target is missing: {generation_dir}")
    return value


@contextmanager
def _switch_lock(root: Path) -> Iterator[None]:
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / _SWITCH_LOCK
    if lock_path.is_symlink():
        raise ValueError(f"knowledge switch lock must not be a symlink: {lock_path}")
    with lock_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _strict_optional_pointer(root: Path, name: str) -> str | None:
    if not (root / name).exists() and not (root / name).is_symlink():
        return None
    return read_pointer(root, name)


def _strict_optional_raw_pointer(root: Path, name: str) -> str | None:
    if not (root / name).exists() and not (root / name).is_symlink():
        return None
    return read_raw_pointer(root, name)


def _unlink_durable(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return
    _fsync_directory(path.parent)


def _write_generation_status(root: Path, generation_id: str, status: str) -> None:
    generation_dir = _validate_generation_directory(root / "generations" / generation_id)
    manifest = load_manifest(generation_dir)
    manifest["status"] = status
    if status == "active":
        manifest["activated_at"] = utc_now()
    atomic_write_text(
        generation_dir / "manifest.json",
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
    )


def _write_switch_journal(root: Path, journal: dict[str, Any]) -> None:
    atomic_write_text(
        root / _SWITCH_JOURNAL,
        json.dumps(journal, ensure_ascii=False, indent=2) + "\n",
    )


def _finish_switch_journal(
    root: Path,
    journal: dict[str, Any],
    *,
    expected: GenerationConfig | None,
) -> None:
    operation = str(journal.get("operation") or "")
    if operation not in {"activate", "rollback"}:
        raise ValueError("switch journal has an invalid operation")
    new_current = _validate_generation_id(str(journal.get("new_current") or ""))
    old_current_raw = journal.get("old_current")
    new_previous_raw = journal.get("new_previous")
    old_current = (
        _validate_generation_id(str(old_current_raw)) if old_current_raw is not None else None
    )
    new_previous = (
        _validate_generation_id(str(new_previous_raw)) if new_previous_raw is not None else None
    )
    if new_current == new_previous:
        raise ValueError("switch journal would make CURRENT equal PREVIOUS")
    target = root / "generations" / new_current
    report = verify_generation(target, expected=expected, full=True)
    if not report["ok"]:
        raise ValueError(f"switch journal target is invalid: {report['errors']}")
    if new_previous is not None:
        previous_report = verify_generation(
            root / "generations" / new_previous,
            # ``expected`` describes only the activation candidate.  An old
            # CURRENT remains rollback-safe when it is internally coherent,
            # even if a release changes model/chunker/runtime revisions.  A
            # rollback intentionally preserves a failed generation as
            # PREVIOUS, so corruption there must not make the safe target
            # unreachable.
            expected=None,
            full=True,
        )
        if operation == "activate" and not previous_report["ok"]:
            raise ValueError(f"switch previous target is invalid: {previous_report['errors']}")

    phase = str(journal.get("phase") or "")
    if phase == "prepared":
        # PREVIOUS is published first as required by the switch contract.  A
        # crash here leaves the old CURRENT untouched and valid; recovery may
        # safely repeat this durable write.
        if new_previous is None:
            _unlink_durable(root / "PREVIOUS")
        else:
            atomic_write_text(root / "PREVIOUS", new_previous + "\n")
        journal["phase"] = "previous_written"
        _write_switch_journal(root, journal)
        phase = "previous_written"

    if phase == "previous_written":
        atomic_write_text(root / "CURRENT", new_current + "\n")
        journal["phase"] = "current_written"
        _write_switch_journal(root, journal)
        phase = "current_written"

    if phase == "current_written":
        _write_generation_status(root, new_current, "active")
        if old_current is not None and old_current != new_current:
            old_report = verify_generation(
                root / "generations" / old_current,
                full=False,
            )
            if old_report["ok"]:
                _write_generation_status(root, old_current, "retired")
        journal["phase"] = "statuses_written"
        _write_switch_journal(root, journal)
        phase = "statuses_written"

    if phase != "statuses_written":
        raise ValueError(f"cannot finish unknown switch journal phase: {phase!r}")
    _unlink_durable(root / _SWITCH_JOURNAL)


def _recover_switch(
    root: Path, *, expected: GenerationConfig | None = None
) -> dict[str, Any] | None:
    journal_path = root / _SWITCH_JOURNAL
    if not journal_path.exists() and not journal_path.is_symlink():
        return None
    if journal_path.is_symlink():
        raise ValueError("knowledge switch journal must not be a symlink")
    try:
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("cannot recover malformed knowledge switch journal") from exc
    if not isinstance(journal, dict) or journal.get("operation") not in {
        "activate",
        "rollback",
    }:
        raise ValueError("invalid knowledge switch journal")
    phase = journal.get("phase")
    if phase not in {"prepared", "previous_written", "current_written", "statuses_written"}:
        raise ValueError("invalid knowledge switch journal phase")
    new_current = _validate_generation_id(str(journal.get("new_current") or ""))
    old_current_raw = journal.get("old_current")
    old_current = (
        _validate_generation_id(str(old_current_raw)) if old_current_raw is not None else None
    )
    old_previous_raw = journal.get("old_previous")
    old_previous = (
        _validate_generation_id(str(old_previous_raw)) if old_previous_raw is not None else None
    )
    current = _strict_optional_raw_pointer(root, "CURRENT")
    allowed_current = (
        {old_current, new_current} if phase in {"prepared", "previous_written"} else {new_current}
    )
    if current not in allowed_current:
        raise ValueError("knowledge switch journal conflicts with CURRENT")
    try:
        _finish_switch_journal(
            root,
            journal,
            expected=expected if journal["operation"] == "activate" else None,
        )
    except Exception:
        # Abort back to the last fully published pair.  CURRENT is restored
        # before the journal is removed, so readers never inherit an invalid
        # target from a failed recovery.
        if old_current is not None:
            old_report = verify_generation(
                root / "generations" / old_current, expected=None, full=True
            )
            if old_report["ok"]:
                atomic_write_text(root / "CURRENT", old_current + "\n")
                if old_previous is None:
                    _unlink_durable(root / "PREVIOUS")
                else:
                    old_previous_report = verify_generation(
                        root / "generations" / old_previous,
                        expected=None,
                        full=True,
                    )
                    if old_previous_report["ok"]:
                        atomic_write_text(root / "PREVIOUS", old_previous + "\n")
                _unlink_durable(journal_path)
        raise
    return {
        "status": "recovered_switch",
        "operation": journal["operation"],
        "generation_id": new_current,
        "previous": journal.get("new_previous"),
    }


def resolve_current_generation(root: Path, *, expected: GenerationConfig | None = None) -> str:
    """Recover an interrupted switch, then return one verified CURRENT id.

    Query/open paths should call this instead of reading ``CURRENT`` directly.
    With no journal this performs no pointer, manifest, or status mutation.
    """
    root = root.expanduser()
    with _switch_lock(root):
        _recover_switch(root, expected=expected)
        current = read_pointer(root, "CURRENT")
        previous = _strict_optional_pointer(root, "PREVIOUS")
        if current == previous:
            raise ValueError("CURRENT and PREVIOUS must not reference the same generation")
        report = verify_generation(
            root / "generations" / current,
            expected=expected,
            full=False,
        )
        if not report["ok"]:
            raise ValueError(f"CURRENT generation is invalid: {report['errors']}")
        return current


def bootstrap_generation(
    root: Path,
    generation_id: str,
    *,
    expected: GenerationConfig,
) -> dict[str, Any]:
    """Seed one inert, fully verified baseline without creating rollback state.

    Bootstrap is deliberately narrower than activation: it is accepted only
    for an empty pointer set and publishes only ``CURRENT``.  A retry after the
    atomic pointer write is idempotent for the exact same generation, while a
    different CURRENT, any PREVIOUS pointer, or a switch journal fails closed.
    Release authorization remains the responsibility of the independent
    release-state gate; this function never creates or opens that state.
    """

    root = root.expanduser()
    generation_id = _validate_generation_id(generation_id)
    with _switch_lock(root):
        journal = root / _SWITCH_JOURNAL
        if journal.exists() or journal.is_symlink():
            raise ValueError("cannot bootstrap while a knowledge switch is pending")
        current = _strict_optional_pointer(root, "CURRENT")
        previous = _strict_optional_pointer(root, "PREVIOUS")
        if previous is not None:
            raise ValueError("bootstrap requires PREVIOUS to be absent")
        if current is not None and current != generation_id:
            raise ValueError("bootstrap requires CURRENT to be absent")

        generation_dir = _validate_generation_directory(
            root / "generations" / generation_id
        )
        report = verify_generation(generation_dir, expected=expected, full=True)
        if not report["ok"]:
            raise ValueError(f"cannot bootstrap invalid generation: {report['errors']}")
        if report["stats"].get("coverage", 0.0) < 0.995:
            raise ValueError("cannot bootstrap generation below 99.5% embedding coverage")
        if current == generation_id:
            return {
                "status": "bootstrapped",
                "generation_id": generation_id,
                "previous": None,
                "idempotent": True,
                "production_authorized": False,
            }

        atomic_write_text(root / "CURRENT", generation_id + "\n")
        return {
            "status": "bootstrapped",
            "generation_id": generation_id,
            "previous": None,
            "idempotent": False,
            "production_authorized": False,
        }


def activate_generation(
    root: Path,
    generation_id: str,
    *,
    expected: GenerationConfig,
    baseline_generation_id: str | None = None,
) -> dict[str, Any]:
    root = root.expanduser()
    generation_id = _validate_generation_id(generation_id)
    if baseline_generation_id is not None:
        baseline_generation_id = _validate_generation_id(baseline_generation_id)
        if baseline_generation_id == generation_id:
            raise ValueError("activation baseline must differ from candidate")
    with _switch_lock(root):
        recovered = _recover_switch(root, expected=expected)
        if (
            recovered
            and recovered["status"] == "recovered_switch"
            and recovered["operation"] == "activate"
            and recovered["generation_id"] == generation_id
        ):
            if (
                baseline_generation_id is not None
                and recovered.get("previous") != baseline_generation_id
            ):
                raise ValueError("activation receipt baseline does not match recovered switch")
            return {
                "status": "active",
                "generation_id": generation_id,
                "previous": recovered.get("previous"),
                "recovered": True,
            }
        generation_dir = _validate_generation_directory(root / "generations" / generation_id)
        report = verify_generation(generation_dir, expected=expected, full=True)
        if not report["ok"]:
            raise ValueError(f"cannot activate invalid generation: {report['errors']}")
        if report["stats"].get("coverage", 0.0) < 0.995:
            raise ValueError("cannot activate generation below 99.5% embedding coverage")
        current = _strict_optional_pointer(root, "CURRENT")
        previous = _strict_optional_pointer(root, "PREVIOUS")
        if current is not None:
            current_report = verify_generation(
                root / "generations" / current, expected=None, full=True
            )
            if not current_report["ok"]:
                raise ValueError(f"existing CURRENT is invalid: {current_report['errors']}")
        if current is not None and current == previous:
            raise ValueError("CURRENT and PREVIOUS must not reference the same generation")
        if current == generation_id:
            if (
                baseline_generation_id is not None
                and previous != baseline_generation_id
            ):
                raise ValueError("activation receipt baseline does not match PREVIOUS")
            return {"status": "active", "generation_id": generation_id, "previous": previous}
        if baseline_generation_id is not None and current != baseline_generation_id:
            raise ValueError("activation receipt baseline no longer matches CURRENT")
        journal = {
            "schema_version": 1,
            "operation": "activate",
            "phase": "prepared",
            "old_current": current,
            "old_previous": previous,
            "new_current": generation_id,
            "new_previous": current,
            "prepared_at": utc_now(),
        }
        _write_switch_journal(root, journal)
        _finish_switch_journal(root, journal, expected=expected)
        return {
            "status": "active",
            "generation_id": generation_id,
            "previous": current,
        }


def rollback_generation(
    root: Path,
    *,
    failed_generation_id: str | None = None,
    safe_generation_id: str | None = None,
) -> dict[str, Any]:
    root = root.expanduser()
    if failed_generation_id is not None:
        failed_generation_id = _validate_generation_id(failed_generation_id)
    if safe_generation_id is not None:
        safe_generation_id = _validate_generation_id(safe_generation_id)
    with _switch_lock(root):
        recovered = _recover_switch(root)
        if (
            recovered
            and recovered["status"] == "recovered_switch"
            and recovered["operation"] == "rollback"
        ):
            if (
                safe_generation_id is not None
                and recovered["generation_id"] != safe_generation_id
            ) or (
                failed_generation_id is not None
                and recovered.get("previous") != failed_generation_id
            ):
                raise ValueError("rollback incident binding does not match recovered switch")
            return {
                "status": "rolled_back",
                "generation_id": recovered["generation_id"],
                "previous": recovered.get("previous"),
                "recovered": True,
            }
        current = read_raw_pointer(root, "CURRENT")
        previous = read_raw_pointer(root, "PREVIOUS")
        if current == previous:
            raise ValueError("CURRENT and PREVIOUS must not reference the same generation")
        if failed_generation_id is not None and current != failed_generation_id:
            raise ValueError("rollback failed generation no longer matches CURRENT")
        if safe_generation_id is not None and previous != safe_generation_id:
            raise ValueError("rollback safe generation no longer matches PREVIOUS")
        for name, generation_id in (("CURRENT", current), ("PREVIOUS", previous)):
            report = verify_generation(root / "generations" / generation_id, full=True)
            if name == "PREVIOUS" and not report["ok"]:
                raise ValueError(f"cannot rollback with invalid {name}: {report['errors']}")
        journal = {
            "schema_version": 1,
            "operation": "rollback",
            "phase": "prepared",
            "old_current": current,
            "old_previous": previous,
            "new_current": previous,
            "new_previous": current,
            "prepared_at": utc_now(),
        }
        _write_switch_journal(root, journal)
        _finish_switch_journal(root, journal, expected=None)
        return {"status": "rolled_back", "generation_id": previous, "previous": current}


def _optional_pointer(root: Path, name: str) -> str | None:
    try:
        return read_pointer(root, name)
    except ValueError:
        return None


ROLLBACK_LIMITS = {
    "coverage_min": 0.99,
    "recall_drop_max": 0.03,
    "stratum_ndcg_drop_max": 0.05,
    "reranker_error_rate_max": 0.02,
    "p95_max_ms": 8000.0,
}


def rollback_reasons(metrics: dict[str, Any]) -> list[str]:
    """Evaluate an already validated chatdaily-knowledge-guard-metrics.v1 snapshot."""

    reasons: list[str] = []
    generation = metrics["generation"]
    verification = metrics["verification"]
    evaluation = metrics["evaluation"]
    runtime = metrics["runtime"]
    source_links = metrics["source_links"]
    if generation["identity_valid"] is False:
        reasons.append("generation_identity_mismatch")
    if generation["manifest_valid"] is False:
        reasons.append("manifest_invalid")
    if float(verification["coverage"]) < ROLLBACK_LIMITS["coverage_min"]:
        reasons.append("coverage_below_99_percent")
    if float(evaluation["recall_at_50"]["drop"]) > ROLLBACK_LIMITS["recall_drop_max"]:
        reasons.append("recall_drop_over_3pp")
    if (
        float(evaluation["stratum_ndcg_at_5"]["max_drop"])
        > ROLLBACK_LIMITS["stratum_ndcg_drop_max"]
    ):
        reasons.append("stratum_ndcg_drop_over_5pp")
    if float(runtime["reranker"]["error_rate"]) > ROLLBACK_LIMITS["reranker_error_rate_max"]:
        reasons.append("reranker_error_rate_over_2_percent")
    if all(
        float(window["p95_ms"]) > ROLLBACK_LIMITS["p95_max_ms"] for window in runtime["p95_windows"]
    ):
        reasons.append("p95_over_8s_three_windows")
    if int(source_links["bad_count"]):
        reasons.append("incorrect_source_link")
    return reasons
