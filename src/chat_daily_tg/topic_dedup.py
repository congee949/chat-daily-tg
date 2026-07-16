"""L2 topic-level dedup for cards about to be pushed to the forum.

The L1 layer (content_seen) catches literal re-forwards — same text or same
bare link. It cannot see the same EVENT arriving re-written: a channel post,
an x_monitor tweet card and a MacRumors item covering one announcement share
no fingerprint. This layer adds a semantic identity:

- ``DeliveredIndex``    — sqlite index of everything already delivered to the
                          forum (ingested back from the tg-cli messages.db,
                          plus write-after-send rows for our own cards), with
                          lazily backfilled embeddings
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
import json
import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from chat_daily_tg import dedup_journal
from chat_daily_tg.evidence_index import cosine_similarity as _cosine_similarity
from chat_daily_tg.paths import DELIVERED_INDEX_DB
from chat_daily_tg.telegram_exporter import canonical_chat_ids, parse_timestamp, sync_chat

log = logging.getLogger(__name__)

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
    embedding TEXT
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

    def __init__(self, path: Path = DELIVERED_INDEX_DB, window_days: int = 14):
        self.window_days = window_days
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), timeout=10.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(_SCHEMA)
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
                        "INSERT OR IGNORE INTO delivered VALUES (?,?,?,?,?,?,NULL)",
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

    def backfill_embeddings(self, embedder, cap: int = 200) -> int:
        """One bounded embed_documents batch over the newest un-embedded rows.

        Failure leaves the rows NULL — they are retried on the next run.
        Returns the number of rows embedded.
        """
        try:
            rows = self._conn.execute(
                "SELECT msg_id, norm_text FROM delivered "
                "WHERE embedding IS NULL AND norm_text != '' "
                "ORDER BY msg_id DESC LIMIT ?",
                (int(cap),),
            ).fetchall()
            if not rows:
                return 0
            vectors = embedder.embed_documents([r["norm_text"] for r in rows])
            if len(vectors) != len(rows):
                raise ValueError(
                    f"embedder returned {len(vectors)} vectors for {len(rows)} rows"
                )
            with self._conn:
                self._conn.executemany(
                    "UPDATE delivered SET embedding = ? WHERE msg_id = ?",
                    [(json.dumps([float(v) for v in vec], separators=(",", ":")), r["msg_id"])
                     for r, vec in zip(rows, vectors)],
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
            emb = (json.dumps([float(v) for v in vector], separators=(",", ":"))
                   if vector else None)
            with self._conn:
                self._conn.executemany(
                    "INSERT OR IGNORE INTO delivered VALUES (?,?,?,?,?,?,?)",
                    [(int(mid), ts, producer, thread_id, text or "", norm, emb)
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
            rows = self._conn.execute(
                "SELECT msg_id, ts, producer, text, norm_text, embedding "
                "FROM delivered WHERE embedding IS NOT NULL AND ts >= ? "
                f"{producer_clause}",
                [cutoff, *excl],
            ).fetchall()
            for r in rows:
                try:
                    vec = [float(v) for v in json.loads(r["embedding"])]
                except (ValueError, TypeError):
                    continue
                out.append(IndexedMsg(
                    msg_id=r["msg_id"], ts=r["ts"], producer=r["producer"],
                    text=r["text"], norm_text=r["norm_text"], vector=vec,
                ))
            return out
        except Exception as e:
            log.warning("L2 recent() failed (%s) — treating as empty", e)
            return []

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
    vector: list[float] | None   # pass to register_sent after a successful send


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
        mode: str = "report",
        candidate_min_sim: float = 0.80,
        strong_sim: float = 0.93,
        retrieval_window_hours: int = 48,
        exclude_producers: frozenset[str] = DEFAULT_EXCLUDE_PRODUCERS,
        max_judge_calls_per_run: int = 5,
        group_internal_id: str = "4424841223",
        ingest: dict | None = None,
    ):
        if mode not in _GATE_MODES:
            log.warning("L2 gate got unknown mode %r; coerced to report", mode)
            mode = "report"
        self.index = index
        self.embedder = embedder
        self.judge = judge
        self.mode = mode
        self.candidate_min_sim = candidate_min_sim
        self.strong_sim = strong_sim
        self.retrieval_window_hours = retrieval_window_hours
        self.exclude_producers = frozenset(exclude_producers)
        self.max_judge_calls_per_run = max_judge_calls_per_run
        self.group_internal_id = str(group_internal_id)
        # Lazy forum ingest: {"db_path":…, "forum_chat_id":…, "sync_limit":…}.
        # Deferring the tg sync + embedding backfill to the first prepare()
        # with actual unseen cards makes a zero-new-card 2-hourly run cost
        # zero network calls.
        self._ingest = dict(ingest) if ingest else None
        self._ingested = False
        self.offline = False
        self._vectors: dict[str, list[float]] = {}
        self._judge_calls = 0
        self._candidates: list[IndexedMsg] | None = None  # per-run retrieval cache

    def _ensure_ingested(self) -> None:
        """Run the deferred forum ingest + embedding backfill exactly once per
        run, and only when something will actually be assessed."""
        if self._ingested or self._ingest is None:
            return
        self._ingested = True
        self.index.ingest_new(
            self._ingest["db_path"], self._ingest["forum_chat_id"],
            sync_limit=self._ingest.get("sync_limit", 300),
        )
        self.index.backfill_embeddings(self.embedder)

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
            for n, v in zip(norms, vectors):
                self._vectors[n] = [float(x) for x in v]
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
                vector = [float(x) for x in self.embedder.embed_queries([norm])[0]]
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
        candidates: list[tuple[float, IndexedMsg]] = []
        for m in self._candidates:
            sim = cosine(vector, m.vector)
            if sim >= self.candidate_min_sim:
                candidates.append((sim, m))
        if not candidates:
            return GateVerdict("deliver", None, 0.0, "substantial", False, "no-match", vector)
        candidates.sort(key=lambda pair: pair[0], reverse=True)
        top = candidates[:3]
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

        judged = verdict is not None and verdict.ok
        if judged:
            if not verdict.same_event:
                base, new_info, reason = "deliver", verdict.new_info, "judge-not-same"
            elif verdict.new_info == "substantial":
                base, new_info, reason = "deliver", "substantial", "judge-substantial"
            elif verdict.new_info == "minor":
                base, new_info, reason = "annotate", "minor", "judge-minor"
            else:  # 'none' — pure re-run of an already delivered event
                base, new_info, reason = "skip", "none", "judge-none"
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

        if self.mode == "report":
            final = "deliver"
        elif self.mode == "annotate":
            final = "annotate"  # skip downgrades to annotate outside enforce
        else:
            final = base

        # Every non-clean decision is journaled — in report mode the would-be
        # action, otherwise the action actually taken.
        self._journal({
            "layer": "L2",
            "action": base if self.mode == "report" else final,
            "mode": self.mode,
            "returned": final,
            "matched_msg_id": best.msg_id,
            "similarity": round(best_sim, 4),
            "new_info": new_info,
            "judged": judged,
            "reason": reason,
            "text_head": text[:100],
            **(ref or {}),
        })
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
        self.index.register_sent(msg_ids, text, producer,
                                 thread_id=thread_id, vector=vector)
        if self._candidates is not None and vector and producer not in self.exclude_producers:
            self._candidates.append(IndexedMsg(
                msg_id=int(msg_ids[0]), ts=_now_utc().isoformat(),
                producer=producer, text=text or "",
                norm_text=normalize_for_embedding(text or ""), vector=vector,
            ))

    @staticmethod
    def _journal(entry: dict) -> None:
        try:
            dedup_journal.record(entry)
        except Exception as e:  # record() itself never raises; belt and braces
            log.warning("L2 journal write failed: %s", e)

    def annotation_html(self, matched_msg_id: int) -> str:
        """Footer line appended to an annotated card (Telegram HTML)."""
        return (
            "🔁 疑似同一事件 · "
            f'<a href="https://t.me/c/{self.group_internal_id}/{matched_msg_id}">前文↗</a>'
        )
