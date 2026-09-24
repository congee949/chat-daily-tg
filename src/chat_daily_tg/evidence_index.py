from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import logging
import math
from pathlib import Path
import re
import sqlite3
import struct
import time
from typing import Protocol

import httpx

from chat_daily_tg.vector_math import CosineScorer, cosine_similarity as cosine_similarity

from chat_daily_tg.inference_queue import (
    CrossProcessInferenceQueue,
    InferenceQueueError,
)

log = logging.getLogger(__name__)

EMBEDDING_NORMALIZATION_VERSION = "chatdaily-normalization-v1"
EMBEDDING_CHUNKER_VERSION = "chatdaily-chunker-v1"

_CLAIM_BULLET_RE = re.compile(r"^-\s+(?:\*\*(?P<title>[^*]+)\*\*[：:])?(?P<body>.+)$")


def _is_bundled_loopback_runtime(endpoint: str) -> bool:
    try:
        url = httpx.URL(str(endpoint))
    except (TypeError, ValueError):
        return False
    return (
        url.scheme == "http"
        and url.host in {"127.0.0.1", "localhost"}
        and url.port == 8790
    )


def _discover_loopback_runtime_revision(endpoint: str, model_path_key: str) -> str:
    """Return the configured local model fingerprint, or empty when unknown.

    Generic OpenAI-compatible providers are not required to expose ChatDaily's
    revision extension. The bundled loopback runtime is: its configured model
    path and response attestations share the local model fingerprint.
    """
    if not _is_bundled_loopback_runtime(endpoint):
        return ""
    runtime_config = Path(
        "~/Library/Application Support/HermesQwenVLRuntime/config/runtime.json"
    ).expanduser()
    try:
        runtime = json.loads(runtime_config.read_text(encoding="utf-8"))
        model_path = Path(runtime[model_path_key]).expanduser()
        from chat_daily_tg.model_identity import model_revision_fingerprint

        return model_revision_fingerprint(model_path)
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return ""


def _strict_expected_revision(value: object) -> str:
    """Normalize an optional response-attestation contract.

    ``unversioned``/``ephemeral`` are explicit absence sentinels used by legacy
    or injected generic providers, not revision values a server can attest.
    """
    if not isinstance(value, str):
        return ""
    revision = value.strip()
    if revision in {"", "unversioned", "ephemeral"}:
        return ""
    return revision


@dataclass(frozen=True)
class EvidenceChunk:
    source_id: str
    source_name: str
    time: str
    sender: str
    text: str


@dataclass(frozen=True)
class EvidenceHit:
    source_id: str
    source_name: str
    time: str
    sender: str
    text: str
    similarity: float


class Embedder(Protocol):
    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        ...

    def embed_queries(self, texts: list[str]) -> list[list[float]]:
        ...


@dataclass(frozen=True)
class RerankResult:
    index: int
    relevance_score: float


class Reranker(Protocol):
    def rerank(
        self, query: str, documents: list[str], *, top_n: int
    ) -> list[RerankResult]:
        ...


class EmbeddingValidationError(ValueError):
    pass


class EmbeddingGenerationMismatch(RuntimeError):
    """The dense index is not wholly readable by the requested generation."""


@dataclass(frozen=True)
class EmbeddingGeneration:
    generation_id: str
    model_id: str
    model_revision: str
    dimension: int
    normalized: bool = True
    query_template: str = "query-v1"
    document_template: str = "document-v1"
    payload_version: str = "text-v1"
    normalization_version: str = EMBEDDING_NORMALIZATION_VERSION
    chunker_version: str = EMBEDDING_CHUNKER_VERSION
    # Query/document encoders are distinct unless a generation explicitly
    # proves otherwise.  Treating a query vector as a stored document vector is
    # unsafe even when the two vectors happen to have the same dimension.
    symmetric_query_document: bool = False
    context_hash: str = ""

    def __post_init__(self) -> None:
        required_identity = {
            "generation_id": self.generation_id,
            "model_id": self.model_id,
            "model_revision": self.model_revision,
            "query_template": self.query_template,
            "document_template": self.document_template,
            "payload_version": self.payload_version,
            "normalization_version": self.normalization_version,
            "chunker_version": self.chunker_version,
        }
        missing = [name for name, value in required_identity.items() if not str(value).strip()]
        if missing:
            raise ValueError(
                "embedding generation identity has empty fields: " + ",".join(missing)
            )
        if self.dimension < 1:
            raise ValueError("embedding generation dimension must be positive")
        identity = self.context_identity()
        computed = hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        if self.context_hash and self.context_hash != computed:
            raise ValueError("embedding context_hash does not match generation identity")
        object.__setattr__(self, "context_hash", computed)

    def context_identity(self) -> dict[str, object]:
        return {
            "model_id": self.model_id,
            "model_revision": self.model_revision,
            "dimension": self.dimension,
            "normalized": self.normalized,
            "query_template": self.query_template,
            "document_template": self.document_template,
            "payload_version": self.payload_version,
            "normalization_version": self.normalization_version,
            "chunker_version": self.chunker_version,
            "symmetric_query_document": self.symmetric_query_document,
        }

    @classmethod
    def from_config(cls, em) -> "EmbeddingGeneration":
        revision = str(getattr(em, "model_revision", "") or "")
        if not revision and getattr(em, "provider", "") == "openai":
            endpoint = str(getattr(em, "endpoint", ""))
            if _is_bundled_loopback_runtime(endpoint):
                revision = _discover_loopback_runtime_revision(
                    endpoint, "embedding_path"
                )
                if not revision:
                    raise ValueError(
                        "bundled loopback embedding revision fingerprint is unavailable"
                    )
            else:
                revision = "unversioned"
        revision = revision or "unversioned"
        identity = {
            "model_id": str(em.model),
            "model_revision": revision,
            "dimension": int(em.dimension),
            "normalized": bool(getattr(em, "normalized", True)),
            "query_template": str(getattr(em, "query_template", "query-v1")),
            "document_template": str(getattr(em, "document_template", "document-v1")),
            "payload_version": str(getattr(em, "payload_version", "text-v1")),
            "normalization_version": str(
                getattr(em, "normalization_version", EMBEDDING_NORMALIZATION_VERSION)
            ),
            "chunker_version": str(
                getattr(em, "chunker_version", EMBEDDING_CHUNKER_VERSION)
            ),
            "symmetric_query_document": bool(
                getattr(em, "symmetric_query_document", False)
            ),
        }
        generation_id = str(getattr(em, "generation_id", "") or "")
        if not generation_id:
            raw = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
            generation_id = f"embedding-{hashlib.sha256(raw).hexdigest()[:20]}"
        return cls(generation_id=generation_id, **identity)


def _render_openai_embedding_text(
    text: str, *, generation: EmbeddingGeneration, role: str
) -> str:
    """Render the declared Qwen text-v1 query/document context exactly once."""
    if generation.payload_version != "text-v1":
        raise EmbeddingValidationError(
            f"unsupported embedding payload version: {generation.payload_version}"
        )
    clean = str(text).strip()
    if role == "query":
        if generation.query_template != "query-v1":
            raise EmbeddingValidationError(
                f"unsupported query template: {generation.query_template}"
            )
        return f"[任务] 检索与下列查询相关的 ChatDaily 内容\n[查询]\n{clean}"
    if role == "document":
        if generation.document_template != "document-v1":
            raise EmbeddingValidationError(
                f"unsupported document template: {generation.document_template}"
            )
        return f"[文档用途] ChatDaily 语义检索候选\n[正文]\n{clean}"
    raise EmbeddingValidationError(f"unsupported embedding role: {role}")


@dataclass(frozen=True)
class EmbeddingCoverage:
    eligible_rows: int
    valid_rows: int
    missing_rows: int
    incompatible_rows: int
    invalid_rows: int

    @property
    def ratio(self) -> float:
        return self.valid_rows / self.eligible_rows if self.eligible_rows else 1.0


GENERATION_METADATA_COLUMNS: tuple[tuple[str, str], ...] = (
    ("generation_id", "TEXT"),
    ("model_id", "TEXT"),
    ("model_revision", "TEXT"),
    ("dimension", "INTEGER"),
    ("normalized", "INTEGER"),
    ("query_template", "TEXT"),
    ("document_template", "TEXT"),
    ("payload_version", "TEXT"),
    ("normalization_version", "TEXT"),
    ("chunker_version", "TEXT"),
    ("symmetric_query_document", "INTEGER"),
    ("context_hash", "TEXT"),
)


def generation_metadata_values(generation: EmbeddingGeneration) -> tuple[object, ...]:
    return (
        generation.generation_id,
        generation.model_id,
        generation.model_revision,
        generation.dimension,
        int(generation.normalized),
        generation.query_template,
        generation.document_template,
        generation.payload_version,
        generation.normalization_version,
        generation.chunker_version,
        int(generation.symmetric_query_document),
        generation.context_hash,
    )


def generation_row_matches(row: object, generation: EmbeddingGeneration) -> bool:
    try:
        return all(
            row[name] == value
            for (name, _definition), value in zip(
                GENERATION_METADATA_COLUMNS,
                generation_metadata_values(generation),
                strict=True,
            )
        )
    except (IndexError, KeyError, TypeError):
        return False


def validate_vector(
    vector: object, *, generation: EmbeddingGeneration
) -> list[float]:
    if not isinstance(vector, (list, tuple)):
        raise EmbeddingValidationError("embedding vector is not a sequence")
    try:
        values = [float(value) for value in vector]
    except (TypeError, ValueError) as exc:
        raise EmbeddingValidationError("embedding vector contains non-numeric values") from exc
    if len(values) != generation.dimension:
        raise EmbeddingValidationError(
            f"embedding dimension {len(values)}, expected {generation.dimension}"
        )
    if not all(math.isfinite(value) for value in values):
        raise EmbeddingValidationError("embedding vector contains nonfinite values")
    norm = math.sqrt(sum(value * value for value in values))
    if norm == 0.0:
        raise EmbeddingValidationError("embedding vector has zero norm")
    if generation.normalized and not 0.90 <= norm <= 1.10:
        raise EmbeddingValidationError(f"embedding vector norm {norm:.6f} is not normalized")
    return values


_TELEGRAM_RE = re.compile(r"^\[Telegram / (?P<group>.+?) / (?P<time>\d{2}:\d{2}) / (?P<sender>.+?)\] (?P<text>.*)$")
_WX_HEADER_RE = re.compile(r"^### \d{4}-\d{2}-\d{2} (?P<time>\d{2}:\d{2})$")
_WX_MESSAGE_RE = re.compile(r"^\*\*(?P<sender>[^*]+)\*\*:\s*(?P<text>.*)$")


def extract_chunks(groups_with_content: list[tuple[str, str]]) -> list[EvidenceChunk]:
    chunks: list[EvidenceChunk] = []
    for source_index, (source_name, content) in enumerate(groups_with_content):
        chunks.extend(_extract_telegram_chunks(source_name, content, source_index=source_index))
        chunks.extend(_extract_wx_chunks(source_name, content, source_index=source_index))
    return chunks


def _extract_telegram_chunks(source_name: str, content: str, *, source_index: int) -> list[EvidenceChunk]:
    chunks: list[EvidenceChunk] = []
    for idx, line in enumerate(content.splitlines()):
        match = _TELEGRAM_RE.match(line.strip())
        if not match:
            continue
        text = match.group("text").strip()
        if not text:
            continue
        group = match.group("group").strip()
        time = match.group("time").strip()
        sender = match.group("sender").strip()
        chunks.append(EvidenceChunk(
            source_id=f"{source_index}#{source_name}#{time}#{idx}",
            source_name=group or source_name,
            time=time,
            sender=sender,
            text=text,
        ))
    return chunks


def _extract_wx_chunks(source_name: str, content: str, *, source_index: int) -> list[EvidenceChunk]:
    chunks: list[EvidenceChunk] = []
    current_time = ""
    for idx, line in enumerate(content.splitlines()):
        stripped = line.strip()
        header = _WX_HEADER_RE.match(stripped)
        if header:
            current_time = header.group("time")
            continue
        match = _WX_MESSAGE_RE.match(stripped)
        if not match:
            continue
        text = match.group("text").strip()
        if not text:
            continue
        chunks.append(EvidenceChunk(
            source_id=f"{source_index}#{source_name}#{current_time}#{idx}",
            source_name=source_name,
            time=current_time,
            sender=match.group("sender").strip(),
            text=text,
        ))
    return chunks


class GeminiEmbeddingError(RuntimeError):
    pass


class GeminiEmbedder:
    @classmethod
    def from_config(cls, em) -> "GeminiEmbedder":
        """The one construction site for every consumer — evidence stage, L2
        topic gate and the calibration script must embed identically, or the
        calibrated thresholds stop describing the shipped gate."""
        import os
        return cls(
            endpoint=em.endpoint, model=em.model,
            api_key=os.environ[em.api_key_env],
            timeout=em.timeout, output_dimensionality=em.dimension,
            batch_size=em.batch_size,
            generation=EmbeddingGeneration.from_config(em),
        )

    def __init__(
        self,
        *,
        endpoint: str,
        model: str,
        api_key: str,
        timeout: float = 120.0,
        output_dimensionality: int | None = None,
        batch_size: int = 100,
        generation: EmbeddingGeneration | None = None,
    ):
        self.endpoint = endpoint.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self.output_dimensionality = output_dimensionality
        self.batch_size = batch_size
        self.generation = generation
        if generation is not None:
            if generation.model_id != self.model:
                raise OpenAIEmbeddingError(
                    "embedding generation model_id does not match requested model"
                )
            if (
                self.output_dimensionality is not None
                and generation.dimension != self.output_dimensionality
            ):
                raise OpenAIEmbeddingError(
                    "embedding generation dimension does not match requested dimension"
                )

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._embed(texts, task_type="RETRIEVAL_DOCUMENT")

    def embed_queries(self, texts: list[str]) -> list[list[float]]:
        return self._embed(texts, task_type="RETRIEVAL_QUERY")

    _RETRYABLE_STATUS = {429, 500, 502, 503, 504}
    _MAX_RETRIES = 10
    _BASE_DELAY = 2.0
    _MAX_DELAY = 60.0  # cap at RPM window; wait for quota reset

    _BATCH_SIZE = 100  # batchEmbedContents accepts at most 100 requests per call
    _INTER_BATCH_DELAY = 2.0  # only between consecutive batches (>100 texts)
    # Any 429 during this _embed call proves the quota is under pressure —
    # later inter-batch gaps fall back to low-frequency pacing (bounded
    # backoff policy: only observed throttling reduces frequency).
    _THROTTLED_INTER_BATCH_DELAY = 16.0

    def _embed(self, texts: list[str], *, task_type: str) -> list[list[float]]:
        if not texts:
            return []
        vectors: list[list[float]] = []
        throttled = False
        with httpx.Client(timeout=self.timeout) as client:
            for i in range(0, len(texts), self.batch_size):
                if i > 0:
                    time.sleep(self._THROTTLED_INTER_BATCH_DELAY if throttled
                               else self._INTER_BATCH_DELAY)
                batch = texts[i : i + self.batch_size]
                batch_vectors, saw_429 = self._embed_batch(client, batch, task_type=task_type)
                throttled = throttled or saw_429
                vectors.extend(batch_vectors)
        return vectors

    def _embed_batch(
        self, client: httpx.Client, texts: list[str], *, task_type: str
    ) -> tuple[list[list[float]], bool]:
        import random

        model_path = f"models/{self.model}"
        requests = []
        for text in texts:
            req: dict[str, object] = {
                "model": model_path,
                "content": {"parts": [{"text": text}]},
                "taskType": task_type,
            }
            if self.output_dimensionality is not None:
                req["outputDimensionality"] = self.output_dimensionality
            requests.append(req)
        body = {"requests": requests}

        last_exc: Exception | None = None
        saw_429 = False
        for attempt in range(self._MAX_RETRIES):
            try:
                response = client.post(
                    f"{self.endpoint}/models/{self.model}:batchEmbedContents",
                    headers={"x-goog-api-key": self.api_key},
                    json=body,
                )
                response.raise_for_status()
                data = response.json()
                embeddings = data.get("embeddings")
                if not isinstance(embeddings, list) or len(embeddings) != len(texts):
                    raise GeminiEmbeddingError(
                        f"batchEmbedContents returned {len(embeddings) if isinstance(embeddings, list) else 0} embeddings, expected {len(texts)}"
                    )
                return [[float(v) for v in e["values"]] for e in embeddings], saw_429
            except httpx.HTTPStatusError as exc:
                last_exc = exc
                status = exc.response.status_code
                saw_429 = saw_429 or status == 429
                if status not in self._RETRYABLE_STATUS:
                    raise GeminiEmbeddingError(
                        f"Gemini batch embedding request failed with HTTP {status}"
                    ) from exc
                delay = min(self._BASE_DELAY * (2 ** attempt), self._MAX_DELAY) + random.uniform(0, 1)
                log.warning("embedding batch 429/5xx (attempt %d/%d, batch_size=%d), retrying in %.1fs",
                            attempt + 1, self._MAX_RETRIES, len(texts), delay)
                time.sleep(delay)
            except (httpx.TimeoutException, httpx.ConnectError) as exc:
                last_exc = exc
                delay = min(self._BASE_DELAY * (2 ** attempt), self._MAX_DELAY) + random.uniform(0, 1)
                log.warning("embedding batch %s (attempt %d/%d, batch_size=%d), retrying in %.1fs",
                            type(exc).__name__, attempt + 1, self._MAX_RETRIES, len(texts), delay)
                time.sleep(delay)
        assert last_exc is not None
        raise GeminiEmbeddingError(
            f"Gemini batch embedding failed after {self._MAX_RETRIES} retries: {last_exc}"
        ) from last_exc


class OpenAIEmbeddingError(RuntimeError):
    pass


class OpenAICompatibleEmbedder:
    """Embedding client for local OpenAI-compatible ``/v1/embeddings`` APIs."""

    @classmethod
    def from_config(cls, em) -> "OpenAICompatibleEmbedder":
        import os

        api_key = os.environ.get(em.api_key_env, "") if em.api_key_env else ""
        return cls(
            endpoint=em.endpoint,
            model=em.model,
            api_key=api_key,
            timeout=em.timeout,
            output_dimensionality=em.dimension,
            batch_size=em.batch_size,
            generation=EmbeddingGeneration.from_config(em),
        )

    def __init__(
        self,
        *,
        endpoint: str,
        model: str,
        api_key: str = "",
        timeout: float = 120.0,
        output_dimensionality: int | None = None,
        batch_size: int = 32,
        generation: EmbeddingGeneration | None = None,
    ):
        self.endpoint = endpoint.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self.output_dimensionality = output_dimensionality
        self.batch_size = batch_size
        self.generation = generation
        self._inference_queue = CrossProcessInferenceQueue(self.endpoint, capacity=64)

    supports_deadline = True

    def embed_documents(
        self, texts: list[str], *, deadline: float | None = None
    ) -> list[list[float]]:
        if self.generation is None:
            raise OpenAIEmbeddingError("embedding generation identity is required")
        rendered = [
            _render_openai_embedding_text(
                text, generation=self.generation, role="document"
            )
            for text in texts
        ]
        return self._embed(rendered, deadline=deadline, online=False)

    def embed_queries(
        self, texts: list[str], *, deadline: float | None = None
    ) -> list[list[float]]:
        if self.generation is None:
            raise OpenAIEmbeddingError("embedding generation identity is required")
        rendered = [
            _render_openai_embedding_text(text, generation=self.generation, role="query")
            for text in texts
        ]
        return self._embed(rendered, deadline=deadline, online=True)

    _RETRYABLE_STATUS = {429, 500, 502, 503, 504}
    _MAX_RETRIES = 3
    _BASE_DELAY = 0.5
    _MAX_DELAY = 4.0

    def _embed(
        self,
        texts: list[str],
        *,
        deadline: float | None = None,
        online: bool = False,
    ) -> list[list[float]]:
        if not texts:
            return []
        absolute_deadline = (
            time.monotonic() + self.timeout if deadline is None else float(deadline)
        )
        vectors: list[list[float]] = []
        with httpx.Client(trust_env=False) as client:
            for i in range(0, len(texts), self.batch_size):
                vectors.extend(
                    self._embed_batch(
                        client,
                        texts[i : i + self.batch_size],
                        deadline=absolute_deadline,
                        online=online,
                    )
                )
        return vectors

    def _embed_batch(
        self,
        client: httpx.Client,
        texts: list[str],
        *,
        deadline: float,
        online: bool = False,
    ) -> list[list[float]]:
        import random

        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        last_exc: Exception | None = None
        for attempt in range(self._MAX_RETRIES):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise OpenAIEmbeddingError("embedding deadline exhausted") from last_exc
            try:
                with self._inference_queue.acquire(
                    online=online,
                    deadline=deadline,
                ):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise OpenAIEmbeddingError("embedding deadline exhausted")
                    response = client.post(
                        f"{self.endpoint}/embeddings",
                        headers=headers,
                        json={
                            "model": self.model,
                            "input": [{"text": text} for text in texts],
                        },
                        timeout=max(0.001, min(float(self.timeout), remaining)),
                    )
                response.raise_for_status()
                return self._parse_response(response.json(), expected=len(texts))
            except InferenceQueueError as exc:
                raise OpenAIEmbeddingError(str(exc)) from exc
            except httpx.HTTPStatusError as exc:
                last_exc = exc
                status = exc.response.status_code
                if status not in self._RETRYABLE_STATUS:
                    raise OpenAIEmbeddingError(
                        f"OpenAI-compatible embedding request failed with HTTP {status}"
                    ) from exc
            except (httpx.TimeoutException, httpx.ConnectError) as exc:
                last_exc = exc
            if attempt + 1 < self._MAX_RETRIES:
                delay = min(self._BASE_DELAY * (2 ** attempt), self._MAX_DELAY)
                delay += random.uniform(0, 0.25)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise OpenAIEmbeddingError("embedding deadline exhausted") from last_exc
                delay = min(delay, remaining)
                log.warning(
                    "local embedding request failed (attempt %d/%d, batch_size=%d), retrying in %.1fs",
                    attempt + 1, self._MAX_RETRIES, len(texts), delay,
                )
                if delay:
                    time.sleep(delay)
        assert last_exc is not None
        raise OpenAIEmbeddingError(
            f"OpenAI-compatible embedding request failed after {self._MAX_RETRIES} retries"
        ) from last_exc

    def _parse_response(self, data: object, *, expected: int) -> list[list[float]]:
        if not isinstance(data, dict) or not isinstance(data.get("data"), list):
            raise OpenAIEmbeddingError("embedding response has no data list")
        if data.get("model") != self.model:
            raise OpenAIEmbeddingError("embedding response model mismatch")
        expected_revision = _strict_expected_revision(
            self.generation.model_revision if self.generation is not None else ""
        )
        if expected_revision:
            actual_revision = data.get("revision")
            if not isinstance(actual_revision, str) or not actual_revision.strip():
                raise OpenAIEmbeddingError(
                    "embedding response revision attestation is missing"
                )
            if actual_revision != expected_revision:
                raise OpenAIEmbeddingError("embedding response revision mismatch")
        items = data["data"]
        if len(items) != expected:
            raise OpenAIEmbeddingError(
                f"embedding response returned {len(items)} vectors, expected {expected}"
            )
        ordered: list[list[float] | None] = [None] * expected
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("embedding"), list):
                raise OpenAIEmbeddingError("embedding response contains an invalid item")
            index = item.get("index")
            if (
                not isinstance(index, int)
                or isinstance(index, bool)
                or not 0 <= index < expected
                or ordered[index] is not None
            ):
                raise OpenAIEmbeddingError("embedding response contains an invalid index")
            try:
                vector = [float(value) for value in item["embedding"]]
            except (TypeError, ValueError) as exc:
                raise OpenAIEmbeddingError("embedding response contains non-numeric values") from exc
            if self.output_dimensionality is not None and len(vector) != self.output_dimensionality:
                raise OpenAIEmbeddingError(
                    f"embedding response dimension {len(vector)}, expected {self.output_dimensionality}"
                )
            if self.generation is not None:
                try:
                    vector = validate_vector(vector, generation=self.generation)
                except EmbeddingValidationError as exc:
                    raise OpenAIEmbeddingError(str(exc)) from exc
            ordered[index] = vector
        if any(vector is None for vector in ordered):
            raise OpenAIEmbeddingError("embedding response is missing an index")
        return [vector for vector in ordered if vector is not None]


class RerankerError(RuntimeError):
    pass


class OpenAICompatibleReranker:
    """Bounded client for the local OpenAI-compatible ``/v1/rerank`` route."""

    @classmethod
    def from_config(cls, cfg) -> "OpenAICompatibleReranker":
        import os

        api_key = os.environ.get(cfg.api_key_env, "") if cfg.api_key_env else ""
        expected_revision = _strict_expected_revision(
            getattr(cfg, "model_revision", "")
            or getattr(cfg, "reranker_revision", "")
        )
        if not expected_revision and _is_bundled_loopback_runtime(str(cfg.endpoint)):
            expected_revision = _discover_loopback_runtime_revision(
                str(cfg.endpoint), "reranker_path"
            )
            if not expected_revision:
                raise RerankerError(
                    "bundled loopback reranker revision fingerprint is unavailable"
                )
        return cls(
            endpoint=cfg.endpoint,
            model=cfg.model,
            api_key=api_key,
            timeout=cfg.timeout,
            expected_revision=expected_revision,
        )

    def __init__(
        self,
        *,
        endpoint: str,
        model: str,
        api_key: str = "",
        timeout: float = 8.0,
        expected_revision: str = "",
    ):
        self.endpoint = endpoint.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self.expected_revision = _strict_expected_revision(expected_revision)
        self._inference_queue = CrossProcessInferenceQueue(self.endpoint, capacity=64)

    def rerank(
        self, query: str, documents: list[str], *, top_n: int
    ) -> list[RerankResult]:
        if not documents:
            return []
        if len(documents) > 64:
            raise RerankerError("reranker candidate count exceeds 64")
        top_n = min(max(1, int(top_n)), len(documents))
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        deadline = time.monotonic() + self.timeout
        try:
            with httpx.Client(timeout=self.timeout, trust_env=False) as client:
                with self._inference_queue.acquire(online=True, deadline=deadline):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise RerankerError("reranker deadline exhausted")
                    response = client.post(
                        f"{self.endpoint}/rerank",
                        headers=headers,
                        json={
                            "model": self.model,
                            "query": query,
                            "documents": documents,
                            "top_n": top_n,
                        },
                        timeout=max(0.001, remaining),
                    )
                response.raise_for_status()
                payload = response.json()
        except InferenceQueueError as exc:
            raise RerankerError(str(exc)) from exc
        except httpx.HTTPStatusError as exc:
            raise RerankerError(
                f"reranker request failed with HTTP {exc.response.status_code}"
            ) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise RerankerError(f"reranker request failed: {type(exc).__name__}") from exc

        raw_results = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(payload, dict) or payload.get("model") != self.model:
            raise RerankerError("reranker response model mismatch")
        if self.expected_revision:
            actual_revision = payload.get("revision")
            if not isinstance(actual_revision, str) or not actual_revision.strip():
                raise RerankerError("reranker response revision attestation is missing")
            if actual_revision != self.expected_revision:
                raise RerankerError("reranker response revision mismatch")
        if not isinstance(raw_results, list):
            raise RerankerError("reranker response has no results list")
        results: list[RerankResult] = []
        seen: set[int] = set()
        for item in raw_results:
            if not isinstance(item, dict):
                raise RerankerError("reranker response contains an invalid result")
            index = item.get("index")
            score = item.get("relevance_score", item.get("score"))
            if (
                not isinstance(index, int)
                or isinstance(index, bool)
                or index < 0
                or index >= len(documents)
                or index in seen
            ):
                raise RerankerError("reranker response contains an invalid index")
            try:
                relevance_score = float(score)
            except (TypeError, ValueError) as exc:
                raise RerankerError("reranker response contains an invalid score") from exc
            if not math.isfinite(relevance_score):
                raise RerankerError("reranker response contains a nonfinite score")
            seen.add(index)
            results.append(RerankResult(index=index, relevance_score=relevance_score))
        if len(results) < top_n:
            raise RerankerError(
                f"reranker returned {len(results)} results, expected at least {top_n}"
            )
        return results[:top_n]


def build_embedder(em) -> Embedder:
    """Build the configured embedding protocol for every runtime consumer."""
    if em.provider == "gemini":
        return GeminiEmbedder.from_config(em)
    if em.provider == "openai":
        return OpenAICompatibleEmbedder.from_config(em)
    raise ValueError(f"unsupported embedding provider: {em.provider}")


def build_reranker(cfg) -> Reranker:
    return OpenAICompatibleReranker.from_config(cfg)


def encode_vector(vec: list[float]) -> bytes:
    """little-endian float32 BLOB — ~4 bytes/dim vs ~12 as JSON text."""
    return struct.pack(f"<{len(vec)}f", *[float(v) for v in vec])


def decode_vector(raw: object) -> list[float] | None:
    """float32 BLOB (new rows) or JSON text (legacy rows) → vector.

    None or anything unparseable → None, so callers keep their skip-the-row
    fail-open behavior regardless of stored format.
    """
    try:
        if isinstance(raw, memoryview):
            raw = bytes(raw)
        if isinstance(raw, bytes):
            if len(raw) % 4:
                return None
            return list(struct.unpack(f"<{len(raw) // 4}f", raw))
        if isinstance(raw, str):
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [float(v) for v in parsed]
        return None
    except (ValueError, TypeError, struct.error):
        return None


class EvidenceIndex:
    def __init__(
        self, path: Path, *, generation: EmbeddingGeneration | None = None
    ):
        self.path = path
        self.generation = generation
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        # embedding stays declared TEXT: sqlite's dynamic typing stores BLOB
        # values in it verbatim, which keeps old-format databases readable.
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS chunks (
                source_id TEXT PRIMARY KEY,
                source_name TEXT NOT NULL,
                time TEXT NOT NULL,
                sender TEXT NOT NULL,
                text TEXT NOT NULL,
                embedding TEXT NOT NULL,
                generation_id TEXT,
                model_id TEXT,
                model_revision TEXT,
                dimension INTEGER,
                normalized INTEGER,
                query_template TEXT,
                document_template TEXT,
                payload_version TEXT,
                normalization_version TEXT,
                chunker_version TEXT,
                symmetric_query_document INTEGER,
                context_hash TEXT
            )
        """)
        columns = {row[1] for row in self.conn.execute("PRAGMA table_info(chunks)")}
        for name, definition in GENERATION_METADATA_COLUMNS:
            if name not in columns:
                self.conn.execute(f"ALTER TABLE chunks ADD COLUMN {name} {definition}")
        self.conn.commit()
        self._rows: list[tuple[tuple[str, str, str, str, str], list[float]]] | None = None
        self._scorer: CosineScorer | None = None

    def close(self) -> None:
        self.conn.close()

    def replace(self, chunks: list[EvidenceChunk], vectors: list[list[float]]) -> None:
        if len(chunks) != len(vectors):
            raise ValueError("chunks and vectors length mismatch")
        if self.generation is not None:
            vectors = [validate_vector(vector, generation=self.generation) for vector in vectors]
        with self.conn:
            self.conn.execute("DELETE FROM chunks")
            metadata_names = ",".join(
                name for name, _definition in GENERATION_METADATA_COLUMNS
            )
            self.conn.executemany(
                "INSERT INTO chunks (source_id,source_name,time,sender,text,embedding,"
                + metadata_names
                + ") VALUES ("
                + ",".join("?" for _ in range(6 + len(GENERATION_METADATA_COLUMNS)))
                + ")",
                [
                    (
                        chunk.source_id,
                        chunk.source_name,
                        chunk.time,
                        chunk.sender,
                        chunk.text,
                        encode_vector(vector),
                        *(generation_metadata_values(self.generation)
                          if self.generation else (None,) * len(GENERATION_METADATA_COLUMNS)),
                    )
                    for chunk, vector in zip(chunks, vectors)
                ],
            )
        self._rows = None
        self._scorer = None

    def search(self, query_vector: list[float], *, top_k: int, min_similarity: float = 0.0) -> list[EvidenceHit]:
        if self.generation is None:
            raise EmbeddingGenerationMismatch(
                "evidence index has no expected embedding generation"
            )
        query_vector = validate_vector(query_vector, generation=self.generation)
        if self._rows is None:
            # Decode the table once per replace() generation: a daily run
            # issues ~12 claim queries against the same immutable snapshot.
            decoded: list[tuple[tuple[str, str, str, str, str], list[float]]] = []
            total_rows = int(self.conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0])
            metadata_names = ",".join(name for name, _ in GENERATION_METADATA_COLUMNS)
            rows = self.conn.execute(
                "SELECT source_id,source_name,time,sender,text,embedding," + metadata_names
                + " FROM chunks"
            ).fetchall()
            for row in rows:
                source_id = row["source_id"]
                if (
                    not generation_row_matches(row, self.generation)
                    or not isinstance(row["embedding"], bytes)
                    or len(row["embedding"]) != self.generation.dimension * 4
                ):
                    raise EmbeddingGenerationMismatch(
                        f"evidence generation is incomplete/incompatible at row {source_id!r}"
                    )
                vector = decode_vector(row["embedding"])
                try:
                    vector = validate_vector(vector, generation=self.generation)
                except EmbeddingValidationError as exc:
                    raise EmbeddingGenerationMismatch(
                        f"evidence row {source_id!r} has an invalid vector"
                    ) from exc
                decoded.append(
                    ((source_id, row["source_name"], row["time"], row["sender"],
                      row["text"]), vector)
                )
            if len(rows) != total_rows:
                raise EmbeddingGenerationMismatch(
                    f"evidence generation row count changed ({len(rows)}/{total_rows})"
                )
            self._scorer = CosineScorer(vector for _, vector in decoded)
            self._rows = decoded
        hits: list[EvidenceHit] = []
        assert self._scorer is not None
        for ((source_id, source_name, timestamp, sender, text), _), similarity in zip(
            self._rows, self._scorer.similarities(query_vector), strict=True
        ):
            if similarity < min_similarity:
                continue
            hits.append(EvidenceHit(
                source_id=source_id,
                source_name=source_name,
                time=timestamp,
                sender=sender,
                text=text,
                similarity=similarity,
            ))
        hits.sort(key=lambda h: h.similarity, reverse=True)
        return hits[:top_k]


def build_evidence_index(
    *,
    index_path: Path,
    groups_with_content: list[tuple[str, str]],
    embedder: Embedder,
) -> EvidenceIndex:
    chunks = filter_evidence_chunks(extract_chunks(groups_with_content))
    vectors = embedder.embed_documents([chunk.text for chunk in chunks])
    generation = getattr(embedder, "generation", None)
    if not isinstance(generation, EmbeddingGeneration) and vectors:
        # Compatibility for injected/test embedders: still create a guarded,
        # single-use generation instead of falling back to untagged rows. Real
        # configured clients always expose their full generation identity.
        dimension = len(vectors[0])
        identity = {
            "model_id": type(embedder).__name__,
            "model_revision": "ephemeral",
            "dimension": dimension,
            "normalized": False,
            "query_template": "ephemeral-query",
            "document_template": "ephemeral-document",
            "payload_version": "text-v1",
            "symmetric_query_document": False,
        }
        raw = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
        generation = EmbeddingGeneration(
            generation_id=f"ephemeral-{hashlib.sha256(raw).hexdigest()[:20]}",
            **identity,
        )
        try:
            embedder.generation = generation
        except Exception:
            pass
    index = EvidenceIndex(
        index_path, generation=generation
    )
    try:
        index.replace(chunks, vectors)
    except Exception:
        index.close()
        raise
    return index


def filter_evidence_chunks(chunks: list[EvidenceChunk]) -> list[EvidenceChunk]:
    keep: list[EvidenceChunk] = []
    high_risk_positions = {idx for idx, chunk in enumerate(chunks) if _is_high_risk_claim(chunk.text)}
    for idx, chunk in enumerate(chunks):
        if idx in high_risk_positions:
            keep.append(chunk)
            continue
        if not _is_contextual_evidence(chunk.text):
            continue
        if idx - 1 in high_risk_positions or idx + 1 in high_risk_positions:
            keep.append(chunk)
    log.info("evidence chunks filtered: kept=%d total=%d", len(keep), len(chunks))
    return keep


def _is_contextual_evidence(text: str) -> bool:
    normalized = text.strip()
    if _is_low_information_message(normalized):
        return False
    contextual_terms = [
        "这个", "那个", "它", "能", "可以", "不能", "读", "看", "入口", "链接",
        "截图", "价格", "额度", "版本", "模型", "活动", "API", "api", "x", "X",
    ]
    return any(term in normalized for term in contextual_terms)


def _is_low_information_message(text: str) -> bool:
    normalized = re.sub(r"\s+", "", text.strip())
    if not normalized:
        return True
    lowered = normalized.lower()
    if lowered in {"ok", "okay", "yes", "no", "嗯", "啊", "哦", "好", "是", "不是"}:
        return True
    if re.fullmatch(r"[哈啊嘿呵hH]+", normalized):
        return True
    return False


def extract_claim_queries(summary_text: str, *, limit: int = 12) -> list[str]:
    queries: list[str] = []
    seen: set[str] = set()
    for line in summary_text.splitlines():
        match = _CLAIM_BULLET_RE.match(line.strip())
        if not match:
            continue
        title = (match.group("title") or "").strip()
        body = match.group("body").strip()
        query = f"{title} {body}".strip()
        query = re.sub(r"（[^）]+ / \d{2}:\d{2}[^）]*）$", "", query).strip()
        if not _is_high_risk_claim(query):
            continue
        if query in seen:
            continue
        seen.add(query)
        queries.append(query)
        if len(queries) >= limit:
            break
    return queries


def build_evidence_context_for_summary(
    *,
    index: EvidenceIndex,
    embedder: Embedder,
    summary_text: str,
    top_k: int,
    min_similarity: float,
    reranker: Reranker | None = None,
    dense_top_k: int | None = None,
) -> str:
    queries = extract_claim_queries(summary_text)
    if not queries:
        return ""
    # Embed all claim queries in one batched request instead of one request per query.
    try:
        query_vectors = embedder.embed_queries(queries)
        if len(query_vectors) != len(queries):
            raise EmbeddingValidationError(
                f"embedder returned {len(query_vectors)} query vectors, "
                f"expected {len(queries)}"
            )
        sections = []
        candidate_count = max(top_k, dense_top_k or top_k)
        for query, vector in zip(queries, query_vectors):
            dense_hits = index.search(
                vector, top_k=candidate_count, min_similarity=min_similarity
            )
            hits = dense_hits[:top_k]
            if reranker is not None and dense_hits:
                try:
                    ranked = reranker.rerank(
                        query, [hit.text for hit in dense_hits], top_n=top_k
                    )
                    expected = min(top_k, len(dense_hits))
                    indexes = [result.index for result in ranked]
                    if (
                        len(indexes) != expected
                        or any(
                            not isinstance(index, int)
                            or isinstance(index, bool)
                            or not 0 <= index < len(dense_hits)
                            for index in indexes
                        )
                        or len(set(indexes)) != len(indexes)
                    ):
                        raise RerankerError(
                            "evidence reranker returned a partial/invalid index mapping"
                        )
                    hits = [dense_hits[index] for index in indexes]
                except Exception as exc:
                    # Reranking is an enhancement over a valid dense generation.
                    # Its failure must retain dense order and never escape into
                    # summary generation or delivery.
                    log.warning("evidence reranker failed; retaining dense order: %s", exc)
            sections.append(f"### Claim 查询：{query}\n{render_evidence_hits(hits)}")
        return "\n\n".join(sections)
    except Exception as exc:
        # Embedding/runtime/queue/SQLite are all optional enhancement layers.
        # Their failure is not evidence that no related content exists. Omit
        # the enhancement entirely so the normal summary path continues
        # without emitting a false "no match" conclusion. Exception is
        # intentionally narrower than BaseException: operator interrupts and
        # process exits must still propagate.
        log.warning(
            "embedding evidence context unavailable; continuing without it: %s",
            exc,
        )
        return ""


def _is_high_risk_claim(text: str) -> bool:
    keywords = [
        "发布", "推出", "涨价", "降价", "封禁", "封锁", "退出", "裁员",
        "额度", "风控", "警告", "验证", "第一", "LiveBench", "版本",
        "Pro", "Plus", "Claude", "Grok", "GPT", "Codex", "Gemini",
        "美元", "元", "免税", "VPN", "政策", "红头文件",
    ]
    return any(keyword in text for keyword in keywords) or bool(re.search(r"\d+(?:\.\d+)+", text))


def retrieve_evidence_for_text(
    *,
    index: EvidenceIndex,
    embedder: Embedder,
    text: str,
    top_k: int,
    min_similarity: float,
) -> list[EvidenceHit]:
    try:
        vectors = embedder.embed_queries([text])
        if not vectors:
            return []
        return index.search(vectors[0], top_k=top_k, min_similarity=min_similarity)
    except Exception as exc:
        # Query-time evidence is optional. Provider, admission-queue and local
        # index failures must never make the caller's primary operation fail or
        # turn an outage into a false "no matching evidence" result.
        log.warning(
            "embedding evidence retrieval unavailable; continuing without it: %s",
            exc,
        )
        return []


def render_evidence_hits(hits: list[EvidenceHit]) -> str:
    if not hits:
        return "(未检索到高相似证据)"
    lines = []
    for hit in hits:
        source = f"{hit.source_name} / {hit.time}" if hit.time else hit.source_name
        sender = f" / {hit.sender}" if hit.sender else ""
        lines.append(f"- [{hit.similarity:.3f}] {source}{sender}: {hit.text}")
    return "\n".join(lines)
