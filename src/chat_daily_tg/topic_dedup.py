"""L2 topic-level dedup for cards about to be pushed to the forum.

The L1 layer (content_seen) catches literal re-forwards — same text or same
bare link. It cannot see the same EVENT arriving re-written: a channel post,
an x_monitor tweet card and a MacRumors item covering one announcement share
no fingerprint. This layer adds a semantic identity:

- ``DeliveredIndex``    — sqlite index of everything already delivered to the
                          forum (ingested back from the tg-cli messages.db,
                          plus write-after-send rows for our own cards), with
                          embeddings backfilled only by an explicit sidecar
- ``guess_producer``    — table-driven producer attribution from card shape
- ``normalize_for_embedding`` — header/URL/timestamp-free text for embedding
- ``SameEventJudge``    — one bounded LLM call deciding 同一事件 + 新增信息量
- ``TopicDedupGate``    — the decision entry point (report/annotate/enforce)

宁可重复，不可误杀 (投递优先于完美): every public entry point fail-opens to
"deliver" — embedder offline, judge garbage, sqlite trouble, anything — and
the strictest default posture is report-only. LLM output is a trust boundary:
the judge verdict goes through a fence-tolerant JSON extractor, bool-ish
coercion and enum coercion whose default (``substantial``) means deliver.
Every non-clean decision is journaled via ``dedup_journal`` so a wrong
suppression is never invisible.
"""
from __future__ import annotations

import dataclasses
import hashlib
import hmac
import json
import logging
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from chat_daily_tg.vector_math import CosineScorer

from chat_daily_tg import dedup_journal
from chat_daily_tg.evidence_index import cosine_similarity as _cosine_similarity
from chat_daily_tg.evidence_index import (
    EmbeddingCoverage,
    EmbeddingGeneration,
    EmbeddingValidationError,
    GENERATION_METADATA_COLUMNS,
    decode_vector,
    encode_vector,
    generation_metadata_values,
    generation_row_matches,
    validate_vector,
)
from chat_daily_tg.paths import DELIVERED_INDEX_DB
from chat_daily_tg.telegram_exporter import canonical_chat_ids, parse_timestamp, sync_chat

log = logging.getLogger(__name__)

L2_CALIBRATION_RECEIPT_SCHEMA = "chatdaily.l2-calibration-receipt.v1"
L2_CALIBRATION_PROTOCOL = "qwen-rerank-top3-same-event-judge-v1"
DEFAULT_CALIBRATION_RECEIPT = DELIVERED_INDEX_DB.with_name(
    "topic-dedup-calibration-receipt.v1.json"
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def calibration_receipt_digest(payload: dict) -> str:
    """Canonical integrity digest, excluding the digest field itself."""
    canonical = dict(payload)
    canonical.pop("receipt_sha256", None)
    raw = json.dumps(
        canonical,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _reject_nonfinite_json(value: str) -> None:
    raise ValueError(f"non-finite JSON number is not permitted: {value}")


def _canonical_json(value: object) -> str:
    """Type-preserving JSON identity used for generation context binding."""
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _component_model_id(component) -> str:
    if component is None:
        return ""
    direct = getattr(component, "model", None)
    if direct:
        return str(direct)
    nested = getattr(component, "llm", None)
    return str(getattr(nested, "model", "") or "")


def _receipt_datetime(value: object) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("receipt timestamp must be a non-empty string")
    parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("receipt timestamp must include a timezone")
    return parsed


def validate_calibration_receipt(
    path: Path | None,
    *,
    generation: EmbeddingGeneration,
    reranker,
    judge,
    candidate_min_sim: float,
    strong_sim: float,
    rerank_top_k: int,
    retrieval_window_hours: int,
    exclude_producers: frozenset[str],
) -> tuple[bool, str | None]:
    """Validate the formal evidence receipt required before L2 suppression.

    The historical markdown calibration report is intentionally not accepted.
    A receipt is a versioned JSON artifact bound to the exact embedding context,
    runtime ranking/judge identities, thresholds and minimum temporal evidence.
    """
    if path is None:
        return False, "calibration_receipt_missing"
    try:
        receipt_path = Path(path)
        if receipt_path.is_symlink() or not receipt_path.is_file():
            return False, "calibration_receipt_missing"
        raw = receipt_path.read_bytes()
        if not raw or len(raw) > 256 * 1024:
            return False, "calibration_receipt_invalid"
        payload = json.loads(raw, parse_constant=_reject_nonfinite_json)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        return False, "calibration_receipt_invalid"
    if not isinstance(payload, dict):
        return False, "calibration_receipt_invalid"
    if (
        payload.get("schema") != L2_CALIBRATION_RECEIPT_SCHEMA
        or payload.get("protocol") != L2_CALIBRATION_PROTOCOL
        or payload.get("status") != "approved"
        or payload.get("approved") is not True
    ):
        return False, "calibration_receipt_invalid"
    supplied_digest = payload.get("receipt_sha256")
    if (
        not isinstance(supplied_digest, str)
        or not _SHA256_RE.fullmatch(supplied_digest)
        or not hmac.compare_digest(supplied_digest, calibration_receipt_digest(payload))
    ):
        return False, "calibration_receipt_invalid"
    if (
        payload.get("generation_id") != generation.generation_id
        or payload.get("context_hash") != generation.context_hash
        or _canonical_json(payload.get("context_identity"))
        != _canonical_json(generation.context_identity())
    ):
        return False, "calibration_context_mismatch"
    reranker_model_id = _component_model_id(reranker)
    judge_model_id = _component_model_id(judge)
    if not reranker_model_id or not judge_model_id:
        return False, "calibration_runtime_identity_missing"
    if (
        payload.get("reranker_model_id") != reranker_model_id
        or payload.get("judge_model_id") != judge_model_id
    ):
        return False, "calibration_model_mismatch"
    receipt_candidate = payload.get("candidate_min_sim")
    receipt_strong = payload.get("strong_sim")
    receipt_rerank_top_k = payload.get("rerank_top_k")
    receipt_window = payload.get("retrieval_window_hours")
    thresholds_match = (
        isinstance(receipt_candidate, (int, float))
        and not isinstance(receipt_candidate, bool)
        and isinstance(receipt_strong, (int, float))
        and not isinstance(receipt_strong, bool)
        and isinstance(receipt_rerank_top_k, int)
        and not isinstance(receipt_rerank_top_k, bool)
        and isinstance(receipt_window, int)
        and not isinstance(receipt_window, bool)
        and abs(float(receipt_candidate) - candidate_min_sim) <= 1e-12
        and abs(float(receipt_strong) - strong_sim) <= 1e-12
        and receipt_rerank_top_k == rerank_top_k
        and receipt_window == retrieval_window_hours
        and payload.get("exclude_producers") == sorted(exclude_producers)
    )
    if not thresholds_match:
        return False, "calibration_policy_mismatch"
    integer_fields = ("labeled_query_count", "incremental_success_days")
    if any(
        not isinstance(payload.get(field), int)
        or isinstance(payload.get(field), bool)
        for field in integer_fields
    ):
        return False, "calibration_evidence_insufficient"
    availability = payload.get("shadow_availability")
    if (
        payload["labeled_query_count"] < 200
        or payload["incremental_success_days"] < 7
        or not isinstance(availability, (int, float))
        or isinstance(availability, bool)
        or not 0.995 <= float(availability) <= 1.0
    ):
        return False, "calibration_evidence_insufficient"
    if any(
        not isinstance(payload.get(field), str)
        or not _SHA256_RE.fullmatch(str(payload.get(field)))
        for field in (
            "gold_set_sha256",
            "evaluation_sha256",
            "shadow_journal_sha256",
        )
    ):
        return False, "calibration_evidence_insufficient"
    try:
        shadow_started = _receipt_datetime(payload.get("shadow_started_at"))
        shadow_completed = _receipt_datetime(payload.get("shadow_completed_at"))
        issued_at = _receipt_datetime(payload.get("issued_at"))
    except ValueError:
        return False, "calibration_evidence_insufficient"
    now = datetime.now(timezone.utc)
    if (
        shadow_completed - shadow_started < timedelta(days=7)
        or issued_at < shadow_completed
        or shadow_completed > now
        or issued_at > now
    ):
        return False, "calibration_evidence_insufficient"
    return True, None

# Producers whose cards never participate in retrieval: alerts and summaries
# aggregate other cards (self-similarity by construction), growth cards quote
# multi-day-old chat, bilibili has its own bvid-level dedup.
DEFAULT_EXCLUDE_PRODUCERS = frozenset({"alert", "daily_summary", "growth", "bilibili"})

_NEW_INFO_ENUM = frozenset({"none", "minor", "substantial"})
_GATE_MODES = frozenset({"report", "annotate", "enforce"})

# Normalized text shorter than this never gates — short cards collide by
# coincidence, not by covering the same event (mirrors content_seen's floor).
_MIN_GATE_CHARS = 24
_MAX_NORM_CHARS = 1500


# --------------------------------------------------------------------------- #
# pure functions

_HHMM = r"[0-2]?\d:[0-5]\d"

# Ordered, first match wins. chatdaily_raw (📢 <频道名> · HH:MM) must precede
# x_monitor (📢 @handle WITHOUT the · HH:MM tail) — both open with 📢.
# bilibili's structural 👤 UP-meta line precedes the loose 日报 title match so
# a video titled 「XX日报」 does not classify as daily_summary.
# macrumors is a best-effort placeholder (link/name sniff); the calibration
# pass over real delivered rows hardens it before enforce mode ever ships.
_PRODUCER_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("alert", re.compile(r"^[⚠🚨✅]")),
    ("chatdaily_raw", re.compile(rf"^📢 .+? · {_HHMM}")),
    ("x_monitor", re.compile(r"^(?:📢 ?@\S+|📄 .*published)")),
    ("growth", re.compile(r"^🌱")),
    ("bilibili", re.compile(r"^👤 .+", re.MULTILINE)),
    # The digest may open with the health-briefing preface (### 🌤️ 个人晨报 …)
    # before its own 日报 title, so the marker is searched across the first few
    # lines, not just line one. Known gap: chunk 2+ of a split digest carries no
    # marker at all and stays 'other' — the calibration report's producer
    # distribution is the check for whether that residue matters.
    ("daily_summary", re.compile(r"^(?:#{0,4}\s*)?(?:📋|🌤|.{0,24}(?:日报|晨报))", re.MULTILINE)),
    ("macrumors", re.compile(r"macrumors", re.IGNORECASE)),
)
_PRODUCER_SNIFF_CHARS = 400  # patterns see only the head — deep-body 日报 mentions don't reclassify


def guess_producer(text: str | None) -> str:
    """Best-effort producer attribution from the card's visible shape.

    Never raises — attribution feeds retrieval exclusion only, and a wrong
    'other' merely means one more row participates in retrieval.
    """
    try:
        body = (text or "").lstrip()[:_PRODUCER_SNIFF_CHARS]
        if not body:
            return "other"
        for name, pattern in _PRODUCER_PATTERNS:
            if pattern.search(body):
                return name
        return "other"
    except Exception:  # pragma: no cover - defensive, guaranteed never to raise
        return "other"


# Markdown links reduce to their label (the label is content, the URL is not);
# then bare URLs and HH:MM stamps go — they are delivery metadata that would
# otherwise dominate similarity between unrelated cards from one channel.
_MD_LINK_RE = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)")
_URL_RE = re.compile(r"https?://\S+")
_HHMM_STAMP_RE = re.compile(rf"(?<!\d){_HHMM}(?!\d)")
_HEADER_LINE_RE = re.compile(r"^(?:📢|📄|🌱|📋|🔁|[⚠🚨✅])")
_META_LINE_RE = re.compile(r"^👤 ")


def normalize_for_embedding(text: str | None) -> str:
    """Header-free, URL-free, timestamp-free body capped at 1500 chars."""
    try:
        raw = (text or "").strip()
        if not raw:
            return ""
        lines = raw.splitlines()
        if lines and _HEADER_LINE_RE.match(lines[0].strip()):
            lines = lines[1:]
        # bilibili puts its 👤 UP-meta on line 2 (after the title), so the
        # meta-line strip cannot be first-line-only.
        lines = [ln for ln in lines if not _META_LINE_RE.match(ln.strip())]
        body = "\n".join(lines)
        body = _MD_LINK_RE.sub(lambda m: m.group(1), body)
        body = _URL_RE.sub(" ", body)
        body = _HHMM_STAMP_RE.sub(" ", body)
        body = re.sub(r"\s+", " ", body).strip()
        return body[:_MAX_NORM_CHARS]
    except Exception:  # pragma: no cover - defensive
        return ""


def cosine(a: list[float] | None, b: list[float] | None) -> float:
    """None/shape-tolerant wrapper over evidence_index.cosine_similarity so the
    L2 gate and the evidence stage can never drift onto different math (the
    calibrated thresholds assume the shared implementation)."""
    if not a or not b or len(a) != len(b):
        return 0.0
    return _cosine_similarity(a, b)


# --------------------------------------------------------------------------- #
# delivered index

_SCHEMA = """
CREATE TABLE IF NOT EXISTS delivered (
    msg_id    INTEGER PRIMARY KEY,
    ts        TEXT NOT NULL,
    producer  TEXT NOT NULL,
    thread_id INTEGER,
    text      TEXT NOT NULL,
    norm_text TEXT NOT NULL,
    embedding TEXT,
    generation_id TEXT,
    model_id TEXT,
    model_revision TEXT,
    dimension INTEGER,
    normalized INTEGER,
    query_template TEXT,
    document_template TEXT,
    payload_version TEXT,
    symmetric_query_document INTEGER,
    context_hash TEXT
);
CREATE INDEX IF NOT EXISTS idx_delivered_ts ON delivered(ts);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class IndexedMsg:
    msg_id: int
    ts: str
    producer: str
    text: str
    norm_text: str
    vector: list[float] | None


class DeliveredIndex:
    """Everything already delivered to the forum, embeddable and retrievable.

    Rows arrive two ways: ``ingest_new`` reads the forum back from the tg-cli
    messages.db (msg_id high-water mark in ``meta['hwm']``), and
    ``register_sent`` writes our own cards immediately after a successful send
    (write-after-send, vector reused from gate time so same-run collisions are
    caught before the next ingest). Embeddings backfill lazily in bounded
    batches; rows without one simply don't participate in retrieval yet.
    """

    def __init__(
        self,
        path: Path = DELIVERED_INDEX_DB,
        window_days: int = 14,
        *,
        generation: EmbeddingGeneration | None = None,
        prune_on_open: bool = True,
    ):
        self.window_days = window_days
        self.generation = generation
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), timeout=10.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(_SCHEMA)
        columns = {row[1] for row in self._conn.execute("PRAGMA table_info(delivered)")}
        for name, definition in GENERATION_METADATA_COLUMNS:
            if name not in columns:
                self._conn.execute(f"ALTER TABLE delivered ADD COLUMN {name} {definition}")
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_delivered_context_ts "
            "ON delivered(generation_id, context_hash, ts)"
        )
        self._conn.execute(
            "INSERT INTO meta(key,value) VALUES('schema_version','2') "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
        )
        columns = {row[1] for row in self._conn.execute("PRAGMA table_info(delivered)")}
        for name in ("mirror_source", "mirror_valid_until"):
            if name not in columns:
                self._conn.execute(f"ALTER TABLE delivered ADD COLUMN {name} TEXT")
        self._conn.commit()
        if prune_on_open:
            self.prune(window_days)

    def close(self) -> None:
        self._conn.close()

    def _get_hwm(self) -> int:
        try:
            row = self._conn.execute("SELECT value FROM meta WHERE key='hwm'").fetchone()
            return int(row["value"]) if row else 0
        except (TypeError, ValueError):
            return 0

    def ingest_new(
        self,
        db_path: str | Path,
        forum_chat_id: str | int,
        sync_limit: int = 300,
        do_sync: bool = True,
    ) -> int:
        """Pull rows newer than the high-water mark out of the tg-cli db.

        Accepts the config form ("-100…") and the bare positive form for
        `forum_chat_id` (canonical_chat_ids covers both). Any failure leaves
        the index usable as-is — retrieval just sees fewer rows this run.
        Returns the number of rows inserted.
        """
        try:
            if do_sync:
                try:
                    sync_chat(str(forum_chat_id), limit=sync_limit)
                except Exception as e:
                    log.warning("L2 ingest: tg sync failed (%s) — continuing with existing rows", e)

            hwm = self._get_hwm()
            ids = sorted(canonical_chat_ids(forum_chat_id))
            placeholders = ",".join("?" for _ in ids)
            src = sqlite3.connect(str(Path(db_path).expanduser()))
            src.row_factory = sqlite3.Row
            try:
                rows = list(src.execute(
                    f"""
                    SELECT msg_id, content, timestamp FROM messages
                    WHERE chat_id IN ({placeholders}) AND msg_id > ?
                    ORDER BY msg_id ASC
                    """,
                    [*ids, hwm],
                ))
            finally:
                src.close()
            if not rows:
                return 0

            inserted = 0
            new_hwm = hwm
            with self._conn:
                for row in rows:
                    msg_id = int(row["msg_id"])
                    new_hwm = max(new_hwm, msg_id)
                    content = (row["content"] or "").strip()
                    if not content:
                        continue  # media-only rows carry nothing to compare
                    self._conn.execute(
                        "INSERT OR IGNORE INTO delivered("
                        "msg_id,ts,producer,thread_id,text,norm_text,embedding,"
                        + ",".join(name for name, _ in GENERATION_METADATA_COLUMNS)
                        + ") VALUES (?,?,?,?,?,?,NULL,"
                        + ",".join("NULL" for _ in GENERATION_METADATA_COLUMNS)
                        + ")",
                        (msg_id, str(row["timestamp"]), guess_producer(content),
                         None, content, normalize_for_embedding(content)),
                    )
                    inserted += 1
                # hwm advances in the SAME transaction as the inserts.
                self._conn.execute(
                    "INSERT INTO meta(key, value) VALUES('hwm', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (str(new_hwm),),
                )
            return inserted
        except Exception as e:
            log.warning("L2 ingest failed (%s) — index stays usable as-is", e)
            return 0

    def ingest_sent_ledger(self, path: str | Path, forum_chat_id: str | int,
                           max_age_hours: float = 24) -> int:
        """Import a validated, fresh snapshot without advancing Telegram's HWM."""
        from chat_daily_tg.sent_content_mirror import read_snapshot
        try:
            snapshot = read_snapshot(Path(path), max_age_hours=max_age_hours)
            target_ids = canonical_chat_ids(forum_chat_id)
            cutoff = (_now_utc() - timedelta(days=self.window_days)).isoformat()
            source = str(Path(path).resolve())
            from chat_daily_tg.sent_content_mirror import utc
            expires = (utc(snapshot["fetched_at"])
                       + timedelta(hours=max_age_hours)).isoformat()
            changed = 0
            with self._conn:
                for row in snapshot["rows"]:
                    if row["chat_id"] not in target_ids or row["sent_at"] < cutoff:
                        continue
                    mid, text = row["message_id"], row["content"]
                    old = self._conn.execute(
                        "SELECT text,norm_text,embedding,producer,mirror_source "
                        "FROM delivered WHERE msg_id=?", (mid,)
                    ).fetchone()
                    if old is not None:
                        if (old["mirror_source"] not in (None, source)
                                or old["producer"] not in ("x_monitor", "macrumors", "unknown")):
                            continue
                    if old is not None and old["mirror_source"] is None and old["embedding"] is not None:
                        # Keep tg-cli provenance: a mirror outage must not hide
                        # independently indexed text or invalidate its vector.
                        self._conn.execute(
                            "UPDATE delivered SET mirror_valid_until=? WHERE msg_id=?",
                            (expires, mid),
                        )
                        continue
                    if old is not None and old["mirror_source"] == source:
                        old_norm = normalize_for_embedding(old["text"])
                        new_norm = normalize_for_embedding(text)
                        if old_norm == new_norm:
                            # Re-imports and formatting-only caption changes are
                            # idempotent, including rows that already have a vector.
                            self._conn.execute(
                                "UPDATE delivered SET mirror_valid_until=? WHERE msg_id=?",
                                (expires, mid),
                            )
                            continue
                    self._conn.execute(
                        "INSERT INTO delivered(msg_id,ts,producer,thread_id,text,norm_text,mirror_source,mirror_valid_until) "
                        "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(msg_id) DO UPDATE SET "
                        "ts=excluded.ts,producer=excluded.producer,thread_id=excluded.thread_id,"
                        "text=excluded.text,norm_text=excluded.norm_text,embedding=NULL,"
                        "mirror_source=excluded.mirror_source,mirror_valid_until=excluded.mirror_valid_until,"
                        + ",".join(f"{name}=NULL" for name, _ in GENERATION_METADATA_COLUMNS),
                        (mid,row["sent_at"],row["producer"],row.get("thread_id"),text,
                         normalize_for_embedding(text),source,expires),
                    )
                    changed += 1
            return changed
        except Exception as exc:
            # A missing or invalid mirror must also disable previously imported rows.
            # Keep their text for audit and re-enable them on the next valid refresh.
            try:
                with self._conn:
                    self._conn.execute(
                        "UPDATE delivered SET mirror_valid_until=? WHERE mirror_source=?",
                        ("1970-01-01T00:00:00+00:00", str(Path(path).resolve())),
                    )
            except sqlite3.Error:
                pass
            log.warning("L2 sent ledger unavailable (%s); delivery remains fail-open", type(exc).__name__)
            return 0

    def backfill_embeddings(
        self,
        embedder,
        cap: int = 200,
        *,
        window_hours: int | None = None,
        deadline: float | None = None,
    ) -> int:
        """One bounded embed_documents batch over the newest un-embedded rows.

        Failure leaves the rows NULL — they are retried on the next run.
        Returns the number of rows embedded.
        """
        try:
            cutoff = (
                (_now_utc() - timedelta(hours=window_hours)).isoformat()
                if window_hours is not None
                else None
            )
            cutoff_clause = "AND ts>=? " if cutoff is not None else ""
            cutoff_args = [cutoff] if cutoff is not None else []
            if self.generation is None:
                log.warning("L2 embedding backfill disabled: no generation identity")
                return 0
            if deadline is not None:
                if not getattr(embedder, "supports_deadline", False):
                    raise RuntimeError("embedding provider does not support an absolute deadline")
                if time.monotonic() >= deadline:
                    raise TimeoutError("embedding deadline exhausted before batch")
            metadata_mismatch = " OR ".join(
                f"{name} IS NOT ?" for name, _ in GENERATION_METADATA_COLUMNS
            )
            rows = self._conn.execute(
                "SELECT msg_id, norm_text FROM delivered WHERE norm_text != '' AND ("
                "embedding IS NULL OR typeof(embedding)!='blob' OR length(embedding)!=? OR "
                + metadata_mismatch + ") " +
                cutoff_clause +
                # MLX pads a batch to its longest input. Grouping outstanding
                # rows by normalized text length keeps bounded backfills much
                # faster without changing row identity or the activity window.
                "AND (mirror_source IS NULL OR julianday(mirror_valid_until)>=julianday('now')) "
                "ORDER BY length(norm_text), msg_id DESC LIMIT ?",
                [
                    self.generation.dimension * 4,
                    *generation_metadata_values(self.generation),
                    *cutoff_args,
                    int(cap),
                ],
            ).fetchall()
            if not rows:
                return 0
            if deadline is not None and getattr(embedder, "supports_deadline", False):
                vectors = embedder.embed_documents(
                    [r["norm_text"] for r in rows], deadline=deadline
                )
            else:
                vectors = embedder.embed_documents([r["norm_text"] for r in rows])
            if len(vectors) != len(rows):
                raise ValueError(
                    f"embedder returned {len(vectors)} vectors for {len(rows)} rows"
                )
            vectors = [
                validate_vector(vector, generation=self.generation) for vector in vectors
            ]
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError("embedding deadline exhausted before commit")
            with self._conn:
                self._conn.executemany(
                    "UPDATE delivered SET embedding=?,"
                    + ",".join(f"{name}=?" for name, _ in GENERATION_METADATA_COLUMNS)
                    + " "
                    "WHERE msg_id=? AND norm_text=?",
                    [
                        (
                            encode_vector(vec),
                            *generation_metadata_values(self.generation),
                            r["msg_id"],
                            r["norm_text"],
                        )
                        for r, vec in zip(rows, vectors)
                    ],
                )
            return len(rows)
        except Exception as e:
            log.warning("L2 embedding backfill failed (%s) — rows stay NULL, retried next run", e)
            return 0

    def register_sent(
        self,
        msg_ids: list[int] | None,
        text: str,
        producer: str,
        thread_id: int | None = None,
        vector: list[float] | None = None,
    ) -> None:
        """Write-after-send rows for our own cards. EVERY member id of an album
        is written (the same rule as raw_seen); empty/None msg_ids is a no-op.
        Failure never blocks anything — the send already happened."""
        if not msg_ids:
            return
        try:
            ts = _now_utc().isoformat()
            norm = normalize_for_embedding(text or "")
            generation = self.generation if vector and self.generation is not None else None
            if vector and generation is not None:
                try:
                    vector = validate_vector(vector, generation=generation)
                except EmbeddingValidationError as exc:
                    # The Telegram send already succeeded.  Keep the delivered
                    # fact with a NULL vector so coverage drops to shadow until
                    # the bounded document backfill repairs it.
                    log.warning(
                        "L2 register_sent rejected invalid vector; row kept unembedded: %s",
                        exc,
                    )
                    vector = None
                    generation = None
            elif vector:
                log.warning(
                    "L2 register_sent ignored unversioned vector; row kept unembedded"
                )
                vector = None
            emb = encode_vector(vector) if vector else None
            with self._conn:
                self._conn.executemany(
                    "INSERT OR IGNORE INTO delivered("
                    "msg_id,ts,producer,thread_id,text,norm_text,embedding,"
                    + ",".join(name for name, _ in GENERATION_METADATA_COLUMNS)
                    + ") VALUES ("
                    + ",".join("?" for _ in range(7 + len(GENERATION_METADATA_COLUMNS)))
                    + ")",
                    [(int(mid), ts, producer, thread_id, text or "", norm, emb,
                      *(generation_metadata_values(generation)
                        if generation else (None,) * len(GENERATION_METADATA_COLUMNS)))
                     for mid in msg_ids],
                )
        except Exception as e:
            log.warning("L2 register_sent failed (delivery already done): %s", e)

    def recent(
        self,
        window_hours: int = 48,
        exclude_producers: frozenset[str] = frozenset(),
    ) -> list[IndexedMsg]:
        """Embedded rows inside the window, minus excluded producers.
        Any read problem returns [] (gate then delivers)."""
        try:
            # Window + producer filters run in SQL so the ~15KB embedding blobs
            # of out-of-window / excluded rows never leave sqlite (all writers
            # stamp ISO UTC +00:00, so the lexicographic ts comparison and the
            # idx_delivered_ts index are both valid).
            cutoff = (_now_utc() - timedelta(hours=window_hours)).isoformat()
            excl = sorted(exclude_producers)
            marks = ",".join("?" for _ in excl)
            producer_clause = f"AND producer NOT IN ({marks})" if excl else ""
            out: list[IndexedMsg] = []
            if self.generation is None:
                log.warning("L2 recent() disabled: no generation identity")
                return []
            rows = self._conn.execute(
                "SELECT msg_id,ts,producer,text,norm_text,embedding,"
                + ",".join(name for name, _ in GENERATION_METADATA_COLUMNS)
                + " FROM delivered WHERE ts >= ? "
                "AND (mirror_source IS NULL OR julianday(mirror_valid_until)>=julianday('now')) "
                "AND typeof(embedding)='blob' AND length(embedding)=? "
                f"{producer_clause}",
                [
                    cutoff,
                    self.generation.dimension * 4,
                    *excl,
                ],
            ).fetchall()
            for r in rows:
                if not generation_row_matches(r, self.generation):
                    continue
                # decode_vector reads both the float32 BLOB rows and legacy
                # JSON rows — the 14-day window prunes the latter out without
                # a forced migration.
                vec = decode_vector(r["embedding"])
                if vec is None:
                    continue
                try:
                    vec = validate_vector(vec, generation=self.generation)
                except EmbeddingValidationError:
                    continue
                out.append(IndexedMsg(
                    msg_id=r["msg_id"], ts=r["ts"], producer=r["producer"],
                    text=r["text"], norm_text=r["norm_text"], vector=vec,
                ))
            return out
        except Exception as e:
            log.warning("L2 recent() failed (%s) — treating as empty", e)
            return []

    def coverage(
        self,
        *,
        window_hours: int,
        exclude_producers: frozenset[str] = frozenset(),
    ) -> EmbeddingCoverage:
        """Coverage over the exact same time/producer domain used by recent()."""
        if self.generation is None:
            return EmbeddingCoverage(0, 0, 0, 0, 0)
        try:
            cutoff = (_now_utc() - timedelta(hours=window_hours)).isoformat()
            excl = sorted(exclude_producers)
            marks = ",".join("?" for _ in excl)
            producer_clause = f"AND producer NOT IN ({marks})" if excl else ""
            rows = self._conn.execute(
                "SELECT embedding,"
                + ",".join(name for name, _ in GENERATION_METADATA_COLUMNS)
                + " FROM delivered "
                 "WHERE ts>=? AND norm_text!='' "
                "AND (mirror_source IS NULL OR julianday(mirror_valid_until)>=julianday('now')) " + producer_clause,
                [cutoff, *excl],
            ).fetchall()
            valid = missing = incompatible = invalid = 0
            for row in rows:
                if row["embedding"] is None:
                    missing += 1
                    continue
                if (
                    not generation_row_matches(row, self.generation)
                    or not isinstance(row["embedding"], bytes)
                    or len(row["embedding"]) != self.generation.dimension * 4
                ):
                    incompatible += 1
                    continue
                vector = decode_vector(row["embedding"])
                try:
                    validate_vector(vector, generation=self.generation)
                except EmbeddingValidationError:
                    invalid += 1
                else:
                    valid += 1
            return EmbeddingCoverage(len(rows), valid, missing, incompatible, invalid)
        except Exception as exc:
            log.warning("L2 coverage failed (%s) — treating coverage as zero", exc)
            return EmbeddingCoverage(1, 0, 0, 0, 1)

    def prune(self, window_days: int | None = None) -> None:
        days = self.window_days if window_days is None else window_days
        try:
            # All writers stamp ISO UTC (+00:00): tg-cli timestamps and our own
            # register_sent rows — lexicographic comparison is therefore safe.
            cutoff = (_now_utc() - timedelta(days=days)).isoformat()
            with self._conn:
                self._conn.execute("DELETE FROM delivered WHERE ts < ?", (cutoff,))
        except Exception as e:
            log.warning("L2 prune failed: %s", e)


# --------------------------------------------------------------------------- #
# same-event judge

@dataclass(frozen=True)
class JudgeVerdict:
    same_event: bool
    new_info: str  # 'none' | 'minor' | 'substantial'
    reason: str
    ok: bool


_JUDGE_SYSTEM = "你是消息去重评审员，判断新卡片与已送达消息是否同一事件，只输出 JSON。"

_JUDGE_PROMPT = """判断下面的「新卡片」与最近已送达的消息是否在讲同一事件，以及新卡片新增了多少实质信息。

## 新卡片
{new_text}

## 最近已送达的消息
{matches_block}

判定标准：
- same_event：是否围绕同一个具体事件/公告/资源。同一主题下的不同事件不算同一事件。
- new_info：新卡片相对已送达内容的新增实质信息量——纯复读=none；补充少量细节=minor；新分析/新数据/新视角/一手信源=substantial。

只输出一个 fenced JSON 对象，不要任何解释：
```json
{{"same_event": true, "new_info": "none|minor|substantial", "reason": "一句话理由"}}
```
"""

_TRUE_STRS = frozenset({"true", "yes", "y", "1", "是", "同一事件", "same"})
_FALSE_STRS = frozenset({"false", "no", "n", "0", "否", "不是", "different"})

_FENCED_JSON_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def _extract_json_object(raw: str) -> dict:
    """Fence-tolerant JSON extraction: bare object → fenced block → first
    '{'..last '}' substring. Raises ValueError when nothing parses."""
    raw = (raw or "").strip()
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            return obj
    except ValueError:
        pass
    m = _FENCED_JSON_RE.search(raw)
    if m:
        try:
            obj = json.loads(m.group(1))
            if isinstance(obj, dict):
                return obj
        except ValueError:
            pass
    start, end = raw.find("{"), raw.rfind("}")
    if start != -1 and end > start:
        obj = json.loads(raw[start:end + 1])
        if isinstance(obj, dict):
            return obj
    raise ValueError("no JSON object found in judge output")


def _coerce_bool(value, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        s = value.strip().lower()
        if s in _TRUE_STRS:
            return True
        if s in _FALSE_STRS:
            return False
    return default


def _coerce_new_info(value) -> str:
    """Out-of-enum → 'substantial': the fail-open default means deliver."""
    if isinstance(value, str):
        s = value.strip().lower()
        if s in _NEW_INFO_ENUM:
            return s
    log.warning("L2 judge emitted out-of-enum new_info %r; coerced to substantial", value)
    return "substantial"


class SameEventJudge:
    """One bounded LLM call per judge(); no retries beyond the client's own.

    `llm` is any LLMClient-compatible object (``chat(prompt, system) ->
    (text, usage)``). model/timeout/max_tokens overrides are applied to a
    dataclasses.replace COPY when the client is a dataclass (the caller's
    shared client is never mutated); non-dataclass fakes get plain setattr.
    """

    def __init__(
        self,
        llm,
        *,
        model: str | None = None,
        timeout: float | None = None,
        max_tokens: int | None = None,
    ):
        overrides = {k: v for k, v in
                     (("model", model), ("timeout", timeout), ("max_tokens", max_tokens))
                     if v is not None}
        if overrides:
            try:
                llm = dataclasses.replace(llm, **overrides)
            except TypeError:
                for key, val in overrides.items():
                    setattr(llm, key, val)
        self.llm = llm

    def _build_prompt(self, new_text: str, matches: list[IndexedMsg]) -> str:
        now = _now_utc()
        blocks = []
        for i, m in enumerate(matches[:3], 1):
            try:
                age_h = max(0.0, (now - parse_timestamp(m.ts)).total_seconds() / 3600)
                age = f"{age_h:.0f} 小时前送达"
            except (ValueError, TypeError):
                age = "送达时间未知"
            blocks.append(f"[{i}] 来源={m.producer} · {age}\n{m.text[:600]}")
        return _JUDGE_PROMPT.format(
            new_text=(new_text or "")[:1200],
            matches_block="\n\n".join(blocks) or "(无)",
        )

    def judge(self, new_text: str, matches: list[IndexedMsg]) -> JudgeVerdict:
        """Any failure (call, parse, anything) → ok=False + substantial, which
        the gate maps to its degraded similarity-only rule."""
        try:
            raw, _usage = self.llm.chat(self._build_prompt(new_text, matches),
                                        system=_JUDGE_SYSTEM)
            parsed = _extract_json_object(raw)
            return JudgeVerdict(
                same_event=_coerce_bool(parsed.get("same_event"), default=False),
                new_info=_coerce_new_info(parsed.get("new_info")),
                reason=str(parsed.get("reason") or ""),
                ok=True,
            )
        except Exception as e:
            log.warning("L2 same-event judge failed (%s) — fail-open substantial", e)
            return JudgeVerdict(same_event=False, new_info="substantial",
                                reason=f"judge-error: {e}", ok=False)


# --------------------------------------------------------------------------- #
# gate

@dataclass(frozen=True)
class GateVerdict:
    action: str                  # 'deliver' | 'annotate' | 'skip'
    matched_msg_id: int | None
    similarity: float
    new_info: str
    judged: bool                 # a judge verdict informed this decision
    reason: str
    # Query vector used for retrieval. register_sent() may reuse it only when
    # the generation explicitly declares query/document symmetry.
    vector: list[float] | None


class TopicDedupGate:
    """Per-run decision gate. One instance per run: the judge budget and the
    offline flag are run-scoped. Callers register_sent(verdict.vector) after
    a successful send so same-run collisions are caught without re-embedding.
    """

    def __init__(
        self,
        index: DeliveredIndex,
        embedder,
        judge: SameEventJudge | None = None,
        *,
        reranker=None,
        jev_shadow=None,
        rerank_top_k: int = 3,
        mode: str = "report",
        candidate_min_sim: float = 0.80,
        strong_sim: float = 0.93,
        retrieval_window_hours: int = 48,
        exclude_producers: frozenset[str] = DEFAULT_EXCLUDE_PRODUCERS,
        max_judge_calls_per_run: int = 5,
        min_embedding_coverage: float = 0.995,
        calibrated_generation_id: str | None = None,
        calibration_receipt_path: Path | None = None,
        online_backfill_cap: int = 32,
        group_internal_id: str = "4424841223",
        ingest: dict | None = None,
    ):
        if mode not in _GATE_MODES:
            log.warning("L2 gate got unknown mode %r; coerced to report", mode)
            mode = "report"
        self.index = index
        self.embedder = embedder
        self.generation = getattr(embedder, "generation", None)
        index_generation = getattr(self.index, "generation", None)
        if index_generation is None and self.generation is not None:
            try:
                self.index.generation = self.generation
            except Exception:
                # Tiny test/degraded adapters may not expose writable state.
                # The public assess() boundary remains fail-open either way.
                pass
        self.judge = judge
        self.jev_shadow = jev_shadow
        self.reranker = reranker
        self.rerank_top_k = min(3, max(1, int(rerank_top_k)))
        self.mode = mode
        self.effective_mode = (
            "report" if mode == "enforce" and reranker is None else mode
        )
        self.min_embedding_coverage = min_embedding_coverage
        self.calibrated_generation_id = calibrated_generation_id
        self.calibration_receipt_path = (
            Path(calibration_receipt_path) if calibration_receipt_path is not None else None
        )
        self.online_backfill_cap = online_backfill_cap
        self.embedding_coverage: EmbeddingCoverage | None = None
        self.mode_downgrade_reason: str | None = (
            "reranker_unavailable"
            if mode == "enforce" and reranker is None
            else None
        )
        self.candidate_min_sim = candidate_min_sim
        self.strong_sim = strong_sim
        self.retrieval_window_hours = retrieval_window_hours
        self.exclude_producers = frozenset(exclude_producers)
        self.max_judge_calls_per_run = max_judge_calls_per_run
        self.group_internal_id = str(group_internal_id)
        # Lazy forum ingest: {"db_path":…, "forum_chat_id":…, "sync_limit":…}.
        # This may copy authoritative text rows into the derived index, but it
        # must never create document embeddings from a query/prepare path.
        # Embedding backfill is an explicit, deadline-bounded sidecar job.
        self._ingest = dict(ingest) if ingest else None
        self._ingested = False
        self.offline = False
        self._vectors: dict[str, list[float]] = {}
        self._judge_calls = 0
        self._candidates: list[IndexedMsg] | None = None  # per-run retrieval cache
        self._scorer: CosineScorer | None = None

    def _ensure_ingested(self) -> None:
        """Copy deferred forum text once, without query-time embedding writes."""
        if self._ingested:
            return
        self._ingested = True
        if self._ingest is not None:
            self.index.ingest_new(
                self._ingest["db_path"], self._ingest["forum_chat_id"],
                sync_limit=self._ingest.get("sync_limit", 300),
            )
        if self._ingest is not None and self._ingest.get("sent_ledger_path"):
            self.index.ingest_sent_ledger(
                self._ingest["sent_ledger_path"], self._ingest["forum_chat_id"],
                max_age_hours=self._ingest.get("sent_ledger_max_age_hours", 24),
            )
        if self.generation is None:
            self.effective_mode = "report"
            self.mode_downgrade_reason = "generation_missing"
        if self.generation is not None:
            self.embedding_coverage = self.index.coverage(
                window_hours=self.retrieval_window_hours,
                exclude_producers=self.exclude_producers,
            )
            coverage_has_gaps = any(
                (
                    self.embedding_coverage.missing_rows,
                    self.embedding_coverage.incompatible_rows,
                    self.embedding_coverage.invalid_rows,
                )
            )
            if (
                self.embedding_coverage.ratio < self.min_embedding_coverage
                or coverage_has_gaps
            ):
                self.effective_mode = "report"
                self.mode_downgrade_reason = "embedding_coverage"
            elif self.mode == "enforce" and self.generation.model_revision.strip().lower() in {
                "",
                "unversioned",
            }:
                self.effective_mode = "report"
                self.mode_downgrade_reason = "model_revision_unversioned"
            elif self.calibrated_generation_id != self.generation.generation_id:
                self.effective_mode = "report"
                self.mode_downgrade_reason = "generation_not_calibrated"
            elif self.mode == "enforce" and self.reranker is None:
                self.effective_mode = "report"
                self.mode_downgrade_reason = "reranker_unavailable"
            elif self.mode == "enforce":
                receipt_ok, receipt_reason = validate_calibration_receipt(
                    self.calibration_receipt_path,
                    generation=self.generation,
                    reranker=self.reranker,
                    judge=self.judge,
                    candidate_min_sim=self.candidate_min_sim,
                    strong_sim=self.strong_sim,
                    rerank_top_k=self.rerank_top_k,
                    retrieval_window_hours=self.retrieval_window_hours,
                    exclude_producers=self.exclude_producers,
                )
                if not receipt_ok:
                    self.effective_mode = "report"
                    self.mode_downgrade_reason = receipt_reason
            if self.effective_mode != self.mode:
                log.warning(
                    "L2 mode downgraded: requested=%s effective=report reason=%s "
                    "generation=%s coverage=%.4f missing=%d incompatible=%d invalid=%d",
                    self.mode,
                    self.mode_downgrade_reason,
                    self.generation.generation_id,
                    self.embedding_coverage.ratio,
                    self.embedding_coverage.missing_rows,
                    self.embedding_coverage.incompatible_rows,
                    self.embedding_coverage.invalid_rows,
                )

    def prepare(self, texts: list[str]) -> None:
        """One embed_queries batch for the run's cards. Failure → offline:
        every assess() this run delivers, one log line total."""
        try:
            norms: list[str] = []
            seen: set[str] = set()
            for t in texts or []:
                n = normalize_for_embedding(t)
                if len(n) >= _MIN_GATE_CHARS and n not in seen and n not in self._vectors:
                    seen.add(n)
                    norms.append(n)
            if not norms:
                return
            self._ensure_ingested()
            vectors = self.embedder.embed_queries(norms)
            if len(vectors) != len(norms):
                raise ValueError(
                    f"embedder returned {len(vectors)} vectors for {len(norms)} texts"
                )
            if self.generation is None:
                raise EmbeddingValidationError("query generation identity is missing")
            validated = [
                validate_vector(vector, generation=self.generation) for vector in vectors
            ]
            for n, vector in zip(norms, validated):
                self._vectors[n] = vector
        except Exception as e:
            self.offline = True
            log.warning("L2 prepare failed (%s) — gate offline this run, all cards deliver", e)

    def assess(self, text: str, ref: dict | None = None) -> GateVerdict:
        """Never raises, never returns anything worse than the mode allows.

        `ref` is the card's own identity ({chat_id, msg_id, channel}) — it goes
        into the journal so a wrong suppression can be recovered with
        --resend CHAT_ID:MSG_ID (the journal is the durable record; without the
        ids it cannot drive the escape hatch)."""
        try:
            return self._assess(text or "", ref)
        except Exception as e:
            log.warning("L2 gate error (%s) — delivering unchecked", e)
            return GateVerdict("deliver", None, 0.0, "substantial", False, "gate-error", None)

    def _assess(self, text: str, ref: dict | None = None) -> GateVerdict:
        norm = normalize_for_embedding(text)
        if len(norm) < _MIN_GATE_CHARS:
            return GateVerdict("deliver", None, 0.0, "substantial", False, "short-text", None)
        if self.offline:
            return GateVerdict("deliver", None, 0.0, "substantial", False, "offline", None)

        vector = self._vectors.get(norm)
        if vector is None:
            try:
                self._ensure_ingested()
                if self.generation is None:
                    raise EmbeddingValidationError("query generation identity is missing")
                query_vectors = self.embedder.embed_queries([norm])
                if len(query_vectors) != 1:
                    raise EmbeddingValidationError(
                        f"embedder returned {len(query_vectors)} query vectors, expected 1"
                    )
                vector = validate_vector(query_vectors[0], generation=self.generation)
                self._vectors[norm] = vector
            except Exception as e:
                self.offline = True
                log.warning("L2 embed failed (%s) — gate offline this run, all cards deliver", e)
                return GateVerdict("deliver", None, 0.0, "substantial", False, "embed-error", None)

        if self._candidates is None:
            # One decoded snapshot per run — recent() pulls ~15KB embedding
            # JSON per row, so per-assess reloads scale O(cards × window).
            # register_sent() appends to this cache to keep same-run
            # collision detection working.
            self._candidates = self.index.recent(
                window_hours=self.retrieval_window_hours,
                exclude_producers=self.exclude_producers)
            self._scorer = CosineScorer(message.vector for message in self._candidates)
        candidates: list[tuple[float, IndexedMsg]] = []
        assert self._scorer is not None
        for m, sim in zip(self._candidates, self._scorer.similarities(vector), strict=True):
            if sim >= self.candidate_min_sim:
                candidates.append((sim, m))
        if not candidates:
            return GateVerdict("deliver", None, 0.0, "substantial", False, "no-match", vector)
        candidates.sort(key=lambda pair: pair[0], reverse=True)
        dense_top = candidates[: self.rerank_top_k]
        top = dense_top
        if self.reranker is not None:
            try:
                ranked = self.reranker.rerank(
                    norm,
                    [message.norm_text for _, message in dense_top],
                    top_n=len(dense_top),
                )
                indexes = [result.index for result in ranked]
                if (
                    len(indexes) != len(dense_top)
                    or any(
                        not isinstance(index, int)
                        or isinstance(index, bool)
                        or not 0 <= index < len(dense_top)
                        for index in indexes
                    )
                    or set(indexes) != set(range(len(dense_top)))
                ):
                    raise ValueError("partial or invalid reranker index mapping")
                top = [dense_top[index] for index in indexes]
            except Exception as exc:
                # A ranker may only reorder SameEventJudge context. Partial,
                # ambiguous or failed mappings preserve the dense top-k. An
                # enforce gate also drops to report immediately: suppression
                # is permitted only after the required rerank stage succeeds.
                log.warning(
                    "L2 reranker failed; retaining dense candidate order: %s", exc
                )
                if self.mode == "enforce":
                    self.effective_mode = "report"
                    self.mode_downgrade_reason = "reranker_unavailable"
                top = dense_top
        best_sim, best = top[0]

        verdict: JudgeVerdict | None = None
        if self.judge is not None and self._judge_calls < self.max_judge_calls_per_run:
            self._judge_calls += 1
            try:
                verdict = self.judge.judge(text, [m for _, m in top])
            except Exception as e:  # a raising judge degrades, never blocks
                log.warning("L2 judge raised (%s) — degraded similarity rule", e)
                verdict = JudgeVerdict(same_event=False, new_info="substantial",
                                       reason=f"judge-raised: {e}", ok=False)

        if self.jev_shadow is not None:
            try:
                self.jev_shadow.observe(text, [m for _, m in top], verdict, ref)
            except Exception as exc:
                log.warning("Jev observer failed error_type=%s", type(exc).__name__)
        judged = verdict is not None and verdict.ok
        if not judged:
            # Reranker relevance is not a same-event probability. Degraded
            # similarity-only behavior must continue to use the dense leader.
            best_sim, best = dense_top[0]
        if judged:
            if not verdict.same_event:
                base, new_info, reason = "deliver", verdict.new_info, "judge-not-same"
            else:
                # Preserve substantial updates. Minor additions keep their card
                # with a link to the first delivery; only no-increment repeats skip.
                reason_map = {
                    "none": "judge-none",
                    "minor": "judge-minor",
                    "substantial": "judge-substantial",
                }
                base = {"none": "skip", "minor": "annotate"}.get(verdict.new_info, "deliver")
                new_info = verdict.new_info
                reason = reason_map.get(verdict.new_info, "judge-same-event")
        else:
            # No judge / budget exhausted / judge failed: similarity-only
            # degraded rule — only a very strong match earns an annotation,
            # and nothing is ever skipped without a judge verdict.
            new_info = "substantial"
            if best_sim >= self.strong_sim:
                base, reason = "annotate", "degraded-strong-sim"
            else:
                base, reason = "deliver", "degraded-below-strong"

        if base == "deliver":
            return GateVerdict("deliver", best.msg_id, best_sim, new_info, judged, reason, vector)

        if self.effective_mode == "report":
            final = "deliver"
        elif self.effective_mode == "annotate":
            final = "annotate"  # skip downgrades to annotate outside enforce
        else:
            final = base

        # Captions cannot establish that distinct images carry no new information.
        if final == "skip" and (ref or {}).get("has_media"):
            final = "annotate"

        # Every non-clean decision is journaled — in report mode the would-be
        # action, otherwise the action actually taken.
        journal_ok = self._journal({
            "layer": "L2",
            "action": base if self.effective_mode == "report" else final,
            "mode": self.effective_mode,
            "requested_mode": self.mode,
            "effective_mode": self.effective_mode,
            "returned": final,
            "matched_msg_id": best.msg_id,
            "similarity": round(best_sim, 4),
            "new_info": new_info,
            "judged": judged,
            "reason": reason,
            "generation_id": (
                self.generation.generation_id if self.generation is not None else None
            ),
            "embedding_coverage": (
                round(self.embedding_coverage.ratio, 6)
                if self.embedding_coverage is not None
                else None
            ),
            "mode_downgrade_reason": self.mode_downgrade_reason,
            "text_head": text[:100],
            **(ref or {}),
        })
        if final == "skip" and not journal_ok:
            final, reason = "deliver", "journal-unavailable"
        return GateVerdict(final, best.msg_id, best_sim, new_info, judged, reason, vector)

    def register_sent(
        self,
        msg_ids: list[int] | None,
        text: str,
        producer: str,
        thread_id: int | None = None,
        vector: list[float] | None = None,
    ) -> None:
        """Write-after-send through the gate so the per-run retrieval cache
        stays coherent (a bare index.register_sent would be invisible to
        same-run assess() calls once the cache is warm)."""
        if not msg_ids:
            return
        document_vector = vector
        if self.generation is not None and vector:
            try:
                if self.generation.symmetric_query_document:
                    document_vector = validate_vector(vector, generation=self.generation)
                else:
                    norm = normalize_for_embedding(text or "")
                    document_vector = validate_vector(
                        self.embedder.embed_documents([norm])[0],
                        generation=self.generation,
                    )
            except Exception as exc:
                document_vector = None
                log.warning(
                    "L2 post-send document embedding failed; delivered row kept without vector: %s",
                    exc,
                )
        self.index.register_sent(
            msg_ids, text, producer, thread_id=thread_id, vector=document_vector
        )
        if (
            self._candidates is not None
            and document_vector
            and producer not in self.exclude_producers
        ):
            assert self._scorer is not None
            self._scorer.append(document_vector)
            self._candidates.append(IndexedMsg(
                msg_id=int(msg_ids[0]), ts=_now_utc().isoformat(),
                producer=producer, text=text or "",
                norm_text=normalize_for_embedding(text or ""), vector=document_vector,
            ))

    @staticmethod
    def _journal(entry: dict) -> bool:
        try:
            return dedup_journal.record(entry) is not False
        except Exception as e:  # record() itself never raises; belt and braces
            log.warning("L2 journal write failed: %s", e)
            return False

    def annotation_html(self, matched_msg_id: int) -> str:
        """Footer line appended to an annotated card (Telegram HTML)."""
        return (
            "🔁 疑似同一事件 · "
            f'<a href="https://t.me/c/{self.group_internal_id}/{matched_msg_id}">前文↗</a>'
        )
