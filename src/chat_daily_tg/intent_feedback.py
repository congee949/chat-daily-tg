"""Append-only read-feedback and intent archive primitives.

This module is deliberately independent from the Telegram delivery ledgers.
It records the *user's interpretation* of already delivered content; it does
not send Telegram messages, consume ``getUpdates``, or resolve source message
IDs from a ledger.  A caller that has a verified mapping passes it as a
:class:`MessageRef`.  If a mapping is not available the event is still useful,
but its source is explicitly marked ``unknown`` rather than being guessed from
the content ID or a URL.

The small API is intended to be used by Hermes or another adapter::

    item = ContentItem("article-42", "AI", "A short article", source=source)
    store = FeedbackStore("/tmp/intent-feedback")
    store.record("expand", content=item, idempotency_key="button-click-1")

Each accepted event is appended to ``events.jsonl`` and to a stable topic /
intent work directory::

    <root>/topics/<topic-slug>/<output-mode>/events.jsonl

The files are append-only and an idempotency key prevents duplicate rows.  No
third-party embedding or model dependency is required.  ``Embedder``,
``Reranker`` and ``TopicClassifier`` are protocols for an optional local
implementation, while :func:`hashed_token_features` provides a tiny offline
feature helper for experiments.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import threading
from typing import Any, Literal, Protocol, runtime_checkable
import unicodedata


SCHEMA = "intent-feedback.v1"
TOPIC_RECLASSIFICATION_SCHEMA = "intent-feedback.topic-reclassification.v1"
EVENT_TYPES = frozenset({"read", "expand", "switch", "filter"})
OUTPUT_MODES = frozenset({"read", "expand", "switch", "filter"})
DEFAULT_WORK_ROOT = Path.home() / "chat-daily" / "intent-feedback"
DEFAULT_TOPIC_TAXONOMY: Mapping[str, tuple[str, ...]] = {
    "AI 与自动化": (
        "人工智能", "ai", "大模型", "语言模型", "llm", "chatgpt", "agent", "智能体",
        "embedding", "向量", "提示词", "prompt", "自动化",
    ),
    "科技与产品": (
        "软件", "硬件", "编程", "开发者", "产品", "科技", "芯片", "机器人",
        "眼镜", "智能硬件", "可穿戴", "apple", "iphone", "ipad", "mac", "android",
        "windows", "开源",
    ),
    "金融与市场": (
        "投资", "股票", "基金", "金融", "市场", "交易", "量化", "期权", "债券",
        "加密货币", "比特币", "以太坊", "经济", "宏观", "估值",
    ),
    "健康与运动": (
        "健康", "运动", "训练", "健身", "营养", "睡眠", "康复", "医学", "疾病",
        "跑步", "力量", "疼痛", "心理",
    ),
    "学习与研究": (
        "学习", "研究", "论文", "教育", "考试", "课程", "读书", "知识", "学术",
        "大学", "教学", "记忆", "写作",
    ),
    "商业与社会": (
        "商业", "创业", "公司", "管理", "营销", "销售", "职场", "招聘", "社会",
        "政策", "法律", "组织", "行业",
    ),
    "文化与内容": (
        "电影", "音乐", "游戏", "文学", "历史", "文化", "艺术", "媒体", "内容创作",
        "短视频", "播客", "摄影", "设计",
    ),
}

EventType = Literal["read", "expand", "switch", "filter"]
OutputMode = Literal["read", "expand", "switch", "filter"]


class IdempotencyConflictError(ValueError):
    """Raised when one idempotency key is reused for a different event."""


class InvalidFeedbackError(ValueError):
    """Raised for malformed event or content fields."""


def _normalise_identifier(value: Any) -> int | str | None:
    """Return a stable Telegram-ish identifier without inventing missing data."""

    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if not text:
        return None
    # Telegram IDs are normally integers.  Preserve non-numeric IDs for
    # adapters that use a stable username or another externally assigned key.
    try:
        return int(text)
    except ValueError:
        return text


def _json_safe(value: Any) -> Any:
    """Convert metadata to JSON-compatible values without failing the archive."""

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (set, frozenset)):
        # Sort by canonical JSON so auto-generated idempotency keys do not
        # depend on Python's per-process hash randomisation.
        items = [_json_safe(item) for item in value]
        return sorted(items, key=_canonical_json)
    return str(value)


def _iso_timestamp(value: datetime | str | None) -> str:
    if value is None:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")
    if isinstance(value, datetime):
        current = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        return current.astimezone(timezone.utc).isoformat(timespec="seconds")
    text = str(value).strip()
    if not text:
        return _iso_timestamp(None)
    # Validate caller-provided timestamps while retaining its exact useful
    # precision and offset.  A malformed timestamp should not enter the log.
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise InvalidFeedbackError(f"occurred_at must be ISO-8601: {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.isoformat(timespec="seconds")


@dataclass(frozen=True)
class MessageRef:
    """A verified source or target Telegram message reference.

    ``chat_id`` and ``message_id`` are intentionally required.  A partial
    mapping is represented as ``None`` by :func:`coerce_message_ref` and is
    stored with ``mapping_status=unknown``.
    """

    chat_id: int | str
    message_id: int | str
    thread_id: int | str | None = None
    kind: str = "telegram"
    url: str | None = None

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "mapping_status": "confirmed",
            "chat_id": self.chat_id,
            "message_id": self.message_id,
        }
        if self.thread_id is not None:
            result["thread_id"] = self.thread_id
        if self.kind:
            result["kind"] = self.kind
        if self.url:
            result["url"] = self.url
        return result


def coerce_message_ref(value: MessageRef | Mapping[str, Any] | None) -> MessageRef | None:
    """Safely coerce a mapping; return ``None`` for an unknown/partial one.

    Only explicit ``chat_id`` + ``message_id`` fields are accepted.  In
    particular, the function never treats ``content_id``, a URL path, or a
    target message as a fabricated source mapping.
    """

    if isinstance(value, MessageRef):
        chat_id = _normalise_identifier(value.chat_id)
        message_id = _normalise_identifier(value.message_id)
        if chat_id is None or message_id is None:
            return None
        return MessageRef(
            chat_id,
            message_id,
            _normalise_identifier(value.thread_id),
            str(value.kind or "telegram").strip() or "telegram",
            str(value.url).strip() if value.url else None,
        )
    if not isinstance(value, Mapping):
        return None
    chat_id = _normalise_identifier(value.get("chat_id"))
    message_id = _normalise_identifier(value.get("message_id"))
    if chat_id is None or message_id is None:
        return None
    thread_id = _normalise_identifier(value.get("thread_id"))
    kind = str(value.get("kind") or value.get("source_kind") or "telegram").strip()
    url_value = value.get("url") or value.get("source_ref")
    url = str(url_value).strip() if url_value else None
    return MessageRef(chat_id, message_id, thread_id, kind or "telegram", url)


@dataclass(frozen=True)
class ContentItem:
    """Content sent to the user and later annotated with a feedback event."""

    content_id: str
    topic: str | None = None
    text: str = ""
    title: str | None = None
    source: MessageRef | Mapping[str, Any] | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.content_id is None or not str(self.content_id).strip():
            raise InvalidFeedbackError("content_id is required")
        if self.text is None:
            object.__setattr__(self, "text", "")
        elif not isinstance(self.text, str):
            object.__setattr__(self, "text", str(self.text))

    @property
    def source_ref(self) -> MessageRef | None:
        return coerce_message_ref(self.source)


@dataclass(frozen=True)
class TopicDecision:
    """Stable topic output from an explicit label or local classifier."""

    label: str
    slug: str
    confidence: float | None = None
    method: str = "explicit"

    @classmethod
    def from_label(
        cls,
        label: str | None,
        *,
        confidence: float | None = None,
        method: str = "explicit",
    ) -> "TopicDecision":
        clean = str(label or "").strip()
        if not clean:
            clean = "unclassified"
            method = "fallback"
        if confidence is None:
            score = None
        else:
            try:
                candidate = float(confidence)
            except (TypeError, ValueError):
                candidate = float("nan")
            score = max(0.0, min(1.0, candidate)) if math.isfinite(candidate) else None
        return cls(clean, topic_slug(clean), score, method)


def topic_slug(label: str | None) -> str:
    """Create a deterministic, path-safe topic directory name."""

    text = unicodedata.normalize("NFKC", str(label or "")).strip().casefold()
    # ``\w`` retains CJK letters and digits while excluding path separators.
    text = re.sub(r"[^\w.-]+", "-", text, flags=re.UNICODE)
    text = re.sub(r"-+", "-", text).strip("-._")
    return text[:80] or "unclassified"


@runtime_checkable
class TopicClassifier(Protocol):
    """Optional local topic classifier; implementations must not call a remote model."""

    def classify(self, text: str, hints: Sequence[str] = ()) -> TopicDecision | str | None:
        ...


@runtime_checkable
class Embedder(Protocol):
    """Pluggable local embedding interface (no implementation/dependency here)."""

    def embed(self, text: str) -> Sequence[float]:
        ...


@runtime_checkable
class Reranker(Protocol):
    """Pluggable local reranker interface (no network semantics)."""

    def rerank(self, query: str, candidates: Sequence[str]) -> Sequence[tuple[str, float]]:
        ...


class KeywordTopicClassifier:
    """Small deterministic classifier useful for offline smoke tests.

    ``topics`` maps a stable topic label to keywords.  Explicit topic labels
    should still be preferred by :func:`record_feedback`; this class is a
    fallback for content that has not yet been classified.
    """

    def __init__(self, topics: Mapping[str, Iterable[str]]) -> None:
        self._topics = {
            str(label).strip(): tuple(str(word).casefold() for word in words if str(word).strip())
            for label, words in topics.items()
            if str(label).strip()
        }

    def classify(self, text: str, hints: Sequence[str] = ()) -> TopicDecision:
        corpus = f"{text} {' '.join(hints)}".casefold()
        scored: list[tuple[int, str]] = []
        for label, words in self._topics.items():
            score = sum(_keyword_occurrences(corpus, word) for word in words if word)
            if score:
                scored.append((score, label))
        if not scored:
            return TopicDecision.from_label(None, method="fallback")
        best_score, best_label = max(scored, key=lambda item: (item[0], item[1]))
        total = sum(score for score, _ in scored)
        return TopicDecision.from_label(
            best_label,
            confidence=best_score / total if total else 0.0,
            method="keyword",
        )


def _keyword_occurrences(corpus: str, keyword: str) -> int:
    """Count a keyword without known token-boundary false positives.

    Latin keywords use word-like boundaries so ``ai`` does not match
    ``training``.  Most Chinese terms intentionally retain substring matching,
    but ``量化`` needs one narrow lexical exception: the product adjective
    ``轻量化`` is not evidence of quantitative finance.
    """
    if re.fullmatch(r"[a-z0-9_+-]+", keyword):
        return len(re.findall(rf"(?<![a-z0-9_]){re.escape(keyword)}(?![a-z0-9_])", corpus))
    if keyword == "量化":
        corpus = corpus.replace("轻量化", "")
    return corpus.count(keyword)


def default_topic_classifier() -> KeywordTopicClassifier:
    """Return the zero-network baseline used while feedback data accumulates."""
    return KeywordTopicClassifier(DEFAULT_TOPIC_TAXONOMY)


def hashed_token_features(text: str, dimensions: int = 128) -> tuple[float, ...]:
    """Return deterministic, dependency-free hashed token features.

    This is an offline feature helper, not a claim of semantic embedding
    quality.  It gives a future local embedding/rerank adapter a stable seam
    without adding a heavyweight model package to the delivery service.
    """

    if dimensions <= 0:
        raise ValueError("dimensions must be positive")
    tokens = re.findall(r"[A-Za-z0-9_]+|[\u3400-\u9fff]", unicodedata.normalize("NFKC", text).casefold())
    vector = [0.0] * dimensions
    for token in tokens:
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
        index = int.from_bytes(digest[:4], "big") % dimensions
        sign = 1.0 if digest[4] & 1 else -1.0
        vector[index] += sign
    norm = math.sqrt(sum(value * value for value in vector))
    if norm:
        vector = [value / norm for value in vector]
    return tuple(vector)


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    """Compute cosine similarity for local features without a numeric dependency."""

    if len(left) != len(right):
        raise ValueError("vectors must have the same length")
    denominator = math.sqrt(sum(x * x for x in left) * sum(y * y for y in right))
    return sum(x * y for x, y in zip(left, right)) / denominator if denominator else 0.0


def _topic_decision(
    topic: str | None,
    text: str,
    classifier: TopicClassifier | None,
    hints: Sequence[str] = (),
) -> TopicDecision:
    if topic and str(topic).strip():
        return TopicDecision.from_label(topic, method="explicit")
    if classifier is not None:
        try:
            result = classifier.classify(text, hints)
        except TypeError:
            # Keep the seam friendly to tiny one-argument local adapters.
            result = classifier.classify(text)  # type: ignore[call-arg]
        if isinstance(result, TopicDecision):
            # Recompute the slug from the label even for an injected
            # classifier.  A plugin-provided slug is not trusted as a path.
            return TopicDecision.from_label(
                result.label,
                confidence=result.confidence,
                method=result.method,
            )
        if isinstance(result, Mapping):
            return TopicDecision.from_label(
                result.get("label") or result.get("topic"),
                confidence=result.get("confidence"),
                method=str(result.get("method") or "classifier"),
            )
        if isinstance(result, str) and result.strip():
            return TopicDecision.from_label(result, method="classifier")
    return TopicDecision.from_label(None, method="fallback")


def _coerce_content(
    content: ContentItem | Mapping[str, Any] | None,
    *,
    content_id: str | None,
    topic: str | None,
    text: str | None,
    title: str | None,
    source: MessageRef | Mapping[str, Any] | None,
    metadata: Mapping[str, Any] | None,
) -> ContentItem:
    if isinstance(content, ContentItem):
        base = content
    elif isinstance(content, Mapping):
        base = ContentItem(
            content_id=str(content.get("content_id") or content.get("id") or ""),
            topic=content.get("topic"),
            text=str(content.get("text") or content.get("content") or ""),
            title=content.get("title"),
            source=content.get("source"),
            metadata=content.get("metadata") or {},
        )
    else:
        base = None
    resolved_id = str(content_id if content_id is not None else (base.content_id if base else "")).strip()
    if not resolved_id:
        raise InvalidFeedbackError("content_id is required")
    return ContentItem(
        content_id=resolved_id,
        topic=topic if topic is not None else (base.topic if base else None),
        text=str(text if text is not None else (base.text if base else "")),
        title=title if title is not None else (base.title if base else None),
        source=source if source is not None else (base.source if base else None),
        metadata=metadata if metadata is not None else (base.metadata if base else {}),
    )


def _mapping_dict(value: MessageRef | Mapping[str, Any] | None) -> dict[str, Any]:
    ref = coerce_message_ref(value)
    return ref.as_dict() if ref is not None else {"mapping_status": "unknown"}


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _event_equivalence(row: Mapping[str, Any]) -> str:
    """Identity payload used to detect explicit-key conflicts.

    ``occurred_at`` may differ on a retry; all semantic fields remain part of
    the comparison so a reused key cannot silently relabel an event.
    """

    return _canonical_json({key: value for key, value in row.items() if key != "occurred_at"})


def _read_event(path: Path, event_id: str) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict) and row.get("event_id") == event_id:
                    return row
    except OSError:
        return None
    return None


def _append_jsonl(path: Path, row: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = _canonical_json(row) + "\n"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(payload)
        handle.flush()
        try:
            os.fsync(handle.fileno())
        except OSError:
            # Some test filesystems and virtual files do not expose fsync.
            pass


def _jsonl_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def _replace_jsonl(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    """Atomically reconcile a materialized topic view under the archive lock."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.tmp-{os.getpid()}-{threading.get_ident()}"
    )
    try:
        descriptor = os.open(
            temporary,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(_canonical_json(row) + "\n")
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:
                pass
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


@dataclass(frozen=True)
class FeedbackResult:
    event: Mapping[str, Any]
    event_id: str
    duplicate: bool
    event_path: Path
    archive_path: Path

    @property
    def created(self) -> bool:
        return not self.duplicate


@dataclass(frozen=True)
class TopicReclassificationResult:
    correction: Mapping[str, Any] | None
    duplicate: bool
    correction_path: Path
    archive_path: Path


class FeedbackStore:
    """Append-only archive rooted at one stable cross-task work directory."""

    def __init__(self, root: str | Path = DEFAULT_WORK_ROOT) -> None:
        self.root = Path(root).expanduser()
        self.event_path = self.root / "events.jsonl"
        self.reclassification_path = self.root / "topic_reclassifications.jsonl"
        self.lock_path = self.root / ".archive.lock"

    @contextmanager
    def _locked_archive(self) -> Iterable[None]:
        """Serialize idempotency checks and both appends across processes.

        Codex tasks, Hermes adapters and the CLI can be separate processes.
        The in-process lock prevents thread races; this advisory file lock
        extends the same critical section across those independent writers.
        """
        self.root.mkdir(parents=True, exist_ok=True)
        flags = (
            os.O_CREAT | os.O_RDWR
            | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptor = os.open(self.lock_path, flags, 0o600)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid():
                raise PermissionError("feedback archive lock must be a user-owned regular file")
            os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    def _archive_path(self, event: Mapping[str, Any]) -> Path:
        topic = event.get("topic") if isinstance(event.get("topic"), Mapping) else {}
        # Slugs are recomputed at the filesystem boundary as a second safety
        # net for custom classifiers or hand-built event dictionaries.
        slug = topic_slug(str(topic.get("slug") or topic.get("label") or "unclassified"))
        mode = str(event.get("output_mode") or event.get("event_type") or "read")
        if mode not in OUTPUT_MODES:
            mode = "read"
        return self.root / "topics" / slug / mode / "events.jsonl"

    def _make_event(
        self,
        event_type: str,
        *,
        content: ContentItem | Mapping[str, Any] | None = None,
        content_id: str | None = None,
        topic: str | None = None,
        text: str | None = None,
        title: str | None = None,
        source: MessageRef | Mapping[str, Any] | None = None,
        target: MessageRef | Mapping[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
        output_mode: str | None = None,
        idempotency_key: str | None = None,
        occurred_at: datetime | str | None = None,
        topic_classifier: TopicClassifier | None = None,
        topic_hints: Sequence[str] = (),
    ) -> dict[str, Any]:
        event_type = str(event_type).strip().casefold()
        if event_type not in EVENT_TYPES:
            raise InvalidFeedbackError(f"event_type must be one of {sorted(EVENT_TYPES)}")
        mode = str(output_mode or event_type).strip().casefold()
        if mode not in OUTPUT_MODES:
            raise InvalidFeedbackError(f"output_mode must be one of {sorted(OUTPUT_MODES)}")
        item = _coerce_content(
            content,
            content_id=content_id,
            topic=topic,
            text=text,
            title=title,
            source=source,
            metadata=metadata,
        )
        decision = _topic_decision(item.topic, item.text, topic_classifier, topic_hints)
        source_mapping = _mapping_dict(item.source)
        target_mapping = _mapping_dict(target)
        body = item.text
        content_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
        semantic: dict[str, Any] = {
            "content_id": item.content_id,
            "event_type": event_type,
            "output_mode": mode,
            "topic": {"label": decision.label, "slug": decision.slug},
            "source": source_mapping,
            "target": target_mapping,
            "content_hash": content_hash,
            "title": item.title,
            "metadata": _json_safe(item.metadata),
        }
        key = str(idempotency_key).strip() if idempotency_key is not None else ""
        if not key:
            key = "auto-" + hashlib.sha256(_canonical_json(semantic).encode("utf-8")).hexdigest()[:32]
        event_id = key
        event: dict[str, Any] = {
            "schema": SCHEMA,
            "event_id": event_id,
            "idempotency_key": key,
            "event_type": event_type,
            "intent": mode,
            "output_mode": mode,
            "output_intensity": mode,
            "occurred_at": _iso_timestamp(occurred_at),
            "content_id": item.content_id,
            "topic": {
                "label": decision.label,
                "slug": decision.slug,
                "method": decision.method,
                "confidence": decision.confidence,
            },
            "source": source_mapping,
            "target": target_mapping,
            "content": {
                "content_id": item.content_id,
                "title": item.title,
                "text": body,
                "content_hash": content_hash,
            },
            "metadata": _json_safe(item.metadata),
        }
        return event

    def record(
        self,
        event_type: str,
        *,
        content: ContentItem | Mapping[str, Any] | None = None,
        content_id: str | None = None,
        topic: str | None = None,
        text: str | None = None,
        title: str | None = None,
        source: MessageRef | Mapping[str, Any] | None = None,
        target: MessageRef | Mapping[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
        output_mode: str | None = None,
        idempotency_key: str | None = None,
        occurred_at: datetime | str | None = None,
        topic_classifier: TopicClassifier | None = None,
        topic_hints: Sequence[str] = (),
    ) -> FeedbackResult:
        event = self._make_event(
            event_type,
            content=content,
            content_id=content_id,
            topic=topic,
            text=text,
            title=title,
            source=source,
            target=target,
            metadata=metadata,
            output_mode=output_mode,
            idempotency_key=idempotency_key,
            occurred_at=occurred_at,
            topic_classifier=topic_classifier,
            topic_hints=topic_hints,
        )
        archive_path = self._archive_path(event)
        event_id = str(event["event_id"])
        with _ARCHIVE_LOCK:
            with self._locked_archive():
                existing = _read_event(self.event_path, event_id)
                archived = _read_event(archive_path, event_id)
                for prior in (existing, archived):
                    if prior is not None and _event_equivalence(prior) != _event_equivalence(event):
                        raise IdempotencyConflictError(
                            f"idempotency key {event_id!r} already identifies a different event"
                        )
                # Repair a partial append after a process interruption, but never
                # emit a second row for the same event ID.
                if existing is None:
                    _append_jsonl(self.event_path, event)
                if archived is None:
                    _append_jsonl(archive_path, event)
                return FeedbackResult(
                    event=existing or archived or event,
                    event_id=event_id,
                    # A partial append is an already accepted event being
                    # repaired, not a new semantic event.
                    duplicate=existing is not None or archived is not None,
                    event_path=self.event_path,
                    archive_path=archive_path,
                )

    # Friendly aliases for adapters that call this operation "append" or
    # "record_feedback".  They intentionally share the same implementation.
    append = record
    append_feedback = record
    record_event = record
    record_feedback = record

    def reclassify_topic(
        self,
        event_id: str,
        topic: str,
        *,
        reason: str,
        occurred_at: datetime | str | None = None,
    ) -> TopicReclassificationResult:
        """Correct one topic projection while preserving append-only facts.

        ``events.jsonl`` remains untouched.  The correction is appended to a
        separate audit log, after which the topic directories are reconciled as
        materialized work views.  A retry of the same correction repairs a
        partially reconciled view without appending a duplicate audit row.
        """
        event_id = str(event_id).strip()
        reason = str(reason).strip()
        if not event_id:
            raise InvalidFeedbackError("event_id is required")
        if not reason:
            raise InvalidFeedbackError("reclassification reason is required")
        new_decision = TopicDecision.from_label(topic, method="correction")
        if new_decision.label == "unclassified":
            raise InvalidFeedbackError("reclassification topic is required")

        with _ARCHIVE_LOCK:
            with self._locked_archive():
                original = _read_event(self.event_path, event_id)
                if original is None:
                    raise InvalidFeedbackError(f"event_id {event_id!r} was not found")
                corrections = [
                    row for row in _jsonl_rows(self.reclassification_path)
                    if row.get("event_id") == event_id
                ]
                original_topic = (
                    original.get("topic")
                    if isinstance(original.get("topic"), Mapping) else {}
                )
                current_topic = (
                    corrections[-1].get("new_topic")
                    if corrections and isinstance(corrections[-1].get("new_topic"), Mapping)
                    else original_topic
                )
                current_label = str(current_topic.get("label") or "unclassified")

                if corrections and current_label == new_decision.label:
                    correction = corrections[-1]
                    duplicate = True
                elif not corrections and current_label == new_decision.label:
                    correction = None
                    duplicate = True
                else:
                    parent_id = str(corrections[-1].get("reclassification_id") or "") if corrections else ""
                    identity = _canonical_json({
                        "event_id": event_id,
                        "old_topic": current_label,
                        "new_topic": new_decision.label,
                        "parent": parent_id,
                    })
                    correction_id = "topic-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]
                    correction = {
                        "schema": TOPIC_RECLASSIFICATION_SCHEMA,
                        "reclassification_id": correction_id,
                        "event_id": event_id,
                        "occurred_at": _iso_timestamp(occurred_at),
                        "old_topic": {
                            "label": current_label,
                            "slug": topic_slug(current_label),
                        },
                        "new_topic": {
                            "label": new_decision.label,
                            "slug": new_decision.slug,
                        },
                        "reason": reason,
                    }
                    _append_jsonl(self.reclassification_path, correction)
                    duplicate = False

                projected = json.loads(_canonical_json(original))
                projected["topic"] = {
                    "label": new_decision.label,
                    "slug": new_decision.slug,
                    "method": "correction",
                    "confidence": None,
                }
                if correction is not None:
                    projected["topic_reclassification"] = {
                        "reclassification_id": correction["reclassification_id"],
                        "original_topic": original_topic,
                    }
                mode = str(projected.get("output_mode") or projected.get("event_type") or "read")
                if mode not in OUTPUT_MODES:
                    mode = "read"
                new_path = self._archive_path(projected)
                topic_root = self.root / "topics"
                for path in topic_root.glob(f"*/{mode}/events.jsonl"):
                    rows = _jsonl_rows(path)
                    retained = [row for row in rows if row.get("event_id") != event_id]
                    if len(retained) != len(rows):
                        _replace_jsonl(path, retained)
                _append_jsonl(new_path, projected)
                return TopicReclassificationResult(
                    correction=correction,
                    duplicate=duplicate,
                    correction_path=self.reclassification_path,
                    archive_path=new_path,
                )

    def iter_events(self) -> Iterable[dict[str, Any]]:
        if not self.event_path.exists():
            return iter(())

        def _rows() -> Iterable[dict[str, Any]]:
            with self.event_path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(row, dict):
                        yield row

        return _rows()


_ARCHIVE_LOCK = threading.Lock()


def record_feedback(
    event_type: str,
    *,
    root: str | Path = DEFAULT_WORK_ROOT,
    **kwargs: Any,
) -> FeedbackResult:
    """Functional convenience wrapper around :class:`FeedbackStore`."""

    return FeedbackStore(root).record(event_type, **kwargs)


# Functional aliases keep integration adapters readable without adding a
# second implementation or a second persistence path.
append_feedback_event = record_feedback
record_intent_feedback = record_feedback


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Record local read/intent feedback events")
    commands = parser.add_subparsers(dest="command", required=True)
    record = commands.add_parser("record", help="append one feedback event")
    record.add_argument("event_type", choices=sorted(EVENT_TYPES))
    record.add_argument("--root", default=str(DEFAULT_WORK_ROOT))
    record.add_argument("--content-id", required=True)
    record.add_argument("--topic")
    record.add_argument("--title")
    record.add_argument("--text", default="")
    record.add_argument("--output-mode", choices=sorted(OUTPUT_MODES))
    record.add_argument("--idempotency-key")
    record.add_argument("--occurred-at")
    for prefix in ("source", "target"):
        record.add_argument(f"--{prefix}-chat-id")
        record.add_argument(f"--{prefix}-message-id")
        record.add_argument(f"--{prefix}-thread-id")
        record.add_argument(f"--{prefix}-kind", default="telegram")
        record.add_argument(f"--{prefix}-url")
    listing = commands.add_parser("list", help="print accepted events as JSONL")
    listing.add_argument("--root", default=str(DEFAULT_WORK_ROOT))
    listing.add_argument("--limit", type=int, default=0, help="newest N events (0 means all)")
    reclassify = commands.add_parser(
        "reclassify-topic",
        help="append an audit correction and reconcile topic work views",
    )
    reclassify.add_argument("--root", default=str(DEFAULT_WORK_ROOT))
    reclassify.add_argument("--event-id", required=True)
    reclassify.add_argument("--topic", required=True)
    reclassify.add_argument("--reason", required=True)
    reclassify.add_argument("--occurred-at")
    return parser


def _cli_ref(args: argparse.Namespace, prefix: str) -> MessageRef | None:
    chat_id = getattr(args, f"{prefix}_chat_id")
    message_id = getattr(args, f"{prefix}_message_id")
    if chat_id is None and message_id is None:
        return None
    # Partial CLI references deliberately become unknown in the event rather
    # than receiving an invented placeholder ID.
    return coerce_message_ref(
        {
            "chat_id": chat_id,
            "message_id": message_id,
            "thread_id": getattr(args, f"{prefix}_thread_id"),
            "kind": getattr(args, f"{prefix}_kind"),
            "url": getattr(args, f"{prefix}_url"),
        }
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    store = FeedbackStore(args.root)
    if args.command == "record":
        result = store.record(
            args.event_type,
            content_id=args.content_id,
            topic=args.topic,
            title=args.title,
            text=args.text,
            output_mode=args.output_mode,
            idempotency_key=args.idempotency_key,
            occurred_at=args.occurred_at,
            source=_cli_ref(args, "source"),
            target=_cli_ref(args, "target"),
        )
        print(json.dumps({"duplicate": result.duplicate, **dict(result.event)}, ensure_ascii=False))
        return 0
    if args.command == "reclassify-topic":
        result = store.reclassify_topic(
            args.event_id,
            args.topic,
            reason=args.reason,
            occurred_at=args.occurred_at,
        )
        print(json.dumps({
            "duplicate": result.duplicate,
            "archive_path": str(result.archive_path),
            "correction": result.correction,
        }, ensure_ascii=False))
        return 0
    rows = list(store.iter_events())
    if args.limit > 0:
        rows = rows[-args.limit :]
    for row in rows:
        print(_canonical_json(row))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through ``python -m``
    raise SystemExit(main())


__all__ = [
    "SCHEMA",
    "TOPIC_RECLASSIFICATION_SCHEMA",
    "EVENT_TYPES",
    "OUTPUT_MODES",
    "DEFAULT_WORK_ROOT",
    "DEFAULT_TOPIC_TAXONOMY",
    "MessageRef",
    "ContentItem",
    "TopicDecision",
    "TopicClassifier",
    "Embedder",
    "Reranker",
    "KeywordTopicClassifier",
    "default_topic_classifier",
    "FeedbackResult",
    "TopicReclassificationResult",
    "FeedbackStore",
    "IdempotencyConflictError",
    "InvalidFeedbackError",
    "coerce_message_ref",
    "topic_slug",
    "hashed_token_features",
    "cosine_similarity",
    "record_feedback",
    "append_feedback_event",
    "record_intent_feedback",
    "main",
]
