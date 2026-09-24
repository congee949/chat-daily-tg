"""All-offline tests for the L2 topic-level dedup module.

No network, no real tg-cli db, no real journal file: the tg sync is
monkeypatched, embeddings come from a deterministic fake, the LLM judge is a
canned object, and dedup_journal.record is captured by an autouse fixture.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from chat_daily_tg import topic_dedup
from chat_daily_tg.sent_content_mirror import write_snapshot
from chat_daily_tg.evidence_index import (
    EmbeddingGeneration,
    GENERATION_METADATA_COLUMNS,
    RerankResult,
    encode_vector,
    generation_metadata_values,
)
from chat_daily_tg.topic_dedup import (
    DeliveredIndex,
    GateVerdict,
    IndexedMsg,
    JudgeVerdict,
    L2_CALIBRATION_PROTOCOL,
    L2_CALIBRATION_RECEIPT_SCHEMA,
    SameEventJudge,
    TopicDedupGate,
    calibration_receipt_digest,
    cosine,
    guess_producer,
    normalize_for_embedding,
)


# --------------------------------------------------------------------------- #
# fixtures & fakes

DELIVERED_TEXT = "某大模型公司今天发布新一代旗舰模型，上下文窗口翻倍，API 价格下调三成，开发者即日可用。"
NEW_TEXT = "刚看到消息：那家大模型公司发布了新旗舰模型，窗口翻倍价格下调，社区反应热烈，值得关注一下。"
TEST_GENERATION = EmbeddingGeneration(
    generation_id="test-generation-v1",
    model_id="qwen-test",
    model_revision="weights-test",
    dimension=2,
    normalized=True,
    query_template="query-v1",
    document_template="document-v1",
    symmetric_query_document=True,
)


def _unit(sim: float) -> list[float]:
    """A unit vector whose cosine against [1, 0] is exactly `sim`."""
    return [sim, math.sqrt(max(0.0, 1.0 - sim * sim))]


class FakeEmbedder:
    """Deterministic: exact-text lookup table of vectors, default [1, 0]."""

    def __init__(self, mapping: dict[str, list[float]] | None = None):
        self.mapping = dict(mapping or {})
        self.default = [1.0, 0.0]
        self.fail = False
        self.query_batches: list[list[str]] = []
        self.batch_size = 32
        self.generation = TEST_GENERATION

    def _lookup(self, texts):
        if self.fail:
            raise RuntimeError("embedder down")
        return [list(self.mapping.get(t, self.default)) for t in texts]

    def embed_documents(self, texts):
        return self._lookup(texts)

    def embed_queries(self, texts):
        self.query_batches.append(list(texts))
        return self._lookup(texts)


class FakeJudge:
    def __init__(self, verdict):
        self.verdict = verdict
        self.model = "judge-test"
        self.calls: list[tuple[str, list]] = []

    def judge(self, new_text, matches):
        self.calls.append((new_text, matches))
        if isinstance(self.verdict, Exception):
            raise self.verdict
        return self.verdict


class IdentityReranker:
    model = "reranker-test"

    def rerank(self, query, documents, *, top_n):
        return [
            RerankResult(index, float(top_n - index))
            for index in range(len(documents))
        ]


class FakeLLM:
    def __init__(self, response):
        self.response = response
        self.prompts: list[str] = []

    def chat(self, prompt, system=None):
        self.prompts.append(prompt)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response, {}


@pytest.fixture(autouse=True)
def journal(monkeypatch):
    """Capture every dedup_journal.record call; nothing touches the real file."""
    captured: list[dict] = []
    monkeypatch.setattr(topic_dedup.dedup_journal, "record",
                        lambda entry, **kw: captured.append(entry))
    return captured


def _index(tmp_path: Path, name: str = "idx.db") -> DeliveredIndex:
    return DeliveredIndex(tmp_path / name, generation=TEST_GENERATION)


def _write_calibration_receipt(
    tmp_path: Path,
    *,
    generation: EmbeddingGeneration = TEST_GENERATION,
    reranker,
    judge,
    candidate_min_sim: float = 0.80,
    strong_sim: float = 0.93,
    rerank_top_k: int = 3,
    retrieval_window_hours: int = 48,
    exclude_producers: frozenset[str] = topic_dedup.DEFAULT_EXCLUDE_PRODUCERS,
    **overrides,
) -> Path:
    payload = {
        "schema": L2_CALIBRATION_RECEIPT_SCHEMA,
        "protocol": L2_CALIBRATION_PROTOCOL,
        "status": "approved",
        "approved": True,
        "generation_id": generation.generation_id,
        "context_hash": generation.context_hash,
        "context_identity": generation.context_identity(),
        "reranker_model_id": getattr(reranker, "model", ""),
        "judge_model_id": getattr(judge, "model", ""),
        "candidate_min_sim": candidate_min_sim,
        "strong_sim": strong_sim,
        "rerank_top_k": rerank_top_k,
        "retrieval_window_hours": retrieval_window_hours,
        "exclude_producers": sorted(exclude_producers),
        "labeled_query_count": 200,
        "shadow_started_at": "2026-08-01T00:00:00+00:00",
        "shadow_completed_at": "2026-08-08T00:00:00+00:00",
        "shadow_availability": 0.995,
        "incremental_success_days": 7,
        "gold_set_sha256": "a" * 64,
        "evaluation_sha256": "b" * 64,
        "shadow_journal_sha256": "c" * 64,
        "issued_at": "2026-08-08T00:00:00+00:00",
    }
    payload.update(overrides)
    payload["receipt_sha256"] = calibration_receipt_digest(payload)
    path = tmp_path / "topic-dedup-calibration-receipt.v1.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _gate(tmp_path, *, mode="annotate", judge=None, sim=0.9, **kw) -> TopicDedupGate:
    """Index with one delivered row at vector [1,0]; new text embeds at `sim`."""
    idx = _index(tmp_path)
    idx.register_sent([100], DELIVERED_TEXT, "chatdaily_raw", None, [1.0, 0.0])
    emb = FakeEmbedder({normalize_for_embedding(NEW_TEXT): _unit(sim)})
    kw.setdefault("calibrated_generation_id", TEST_GENERATION.generation_id)
    if mode == "enforce":
        kw.setdefault("reranker", IdentityReranker())
        reranker = kw.get("reranker")
        if reranker is not None and not getattr(reranker, "model", None):
            reranker.model = "reranker-test"
        if (
            reranker is not None
            and judge is not None
            and "calibration_receipt_path" not in kw
        ):
            kw["calibration_receipt_path"] = _write_calibration_receipt(
                tmp_path,
                reranker=reranker,
                judge=judge,
                candidate_min_sim=kw.get("candidate_min_sim", 0.80),
                strong_sim=kw.get("strong_sim", 0.93),
                rerank_top_k=kw.get("rerank_top_k", 3),
                retrieval_window_hours=kw.get("retrieval_window_hours", 48),
                exclude_producers=frozenset(
                    kw.get("exclude_producers", topic_dedup.DEFAULT_EXCLUDE_PRODUCERS)
                ),
            )
    return TopicDedupGate(idx, emb, judge, mode=mode, **kw)


# --------------------------------------------------------------------------- #
# guess_producer

def test_guess_producer_chatdaily_raw_header():
    assert guess_producer("📢 测试频道 · 09:30\n今天发布了新的开源项目。") == "chatdaily_raw"


def test_guess_producer_x_monitor_handle_and_article():
    assert guess_producer("📢 @elonmusk\n刚刚发推说要开源全部权重。") == "x_monitor"
    assert guess_producer("📄 New article published: scaling laws revisited") == "x_monitor"


def test_guess_producer_alert_shapes():
    assert guess_producer("⚠️ 磁盘空间不足") == "alert"
    assert guess_producer("🚨 launchd job 连续失败") == "alert"
    assert guess_producer("✅ 心跳恢复正常") == "alert"


def test_guess_producer_bilibili_up_line():
    assert guess_producer("这是一个视频标题\n👤 某UP主 · 12.3万播放") == "bilibili"


def test_guess_producer_growth_and_daily_summary():
    assert guess_producer("🌱 如何建立长期主义习惯\n\n📌 要点") == "growth"
    assert guess_producer("📋 2026-07-16 日报") == "daily_summary"
    assert guess_producer("07-16 日报 · 今日要点") == "daily_summary"


def test_guess_producer_macrumors_placeholder():
    assert guess_producer("Apple 发布新品 https://www.macrumors.com/2026/07/x/") == "macrumors"


def test_guess_producer_garbage_never_raises():
    assert guess_producer("随便说点什么，没有任何标记。") == "other"
    assert guess_producer("") == "other"
    assert guess_producer(None) == "other"
    assert guess_producer("\x00\xff garbled \ud83d") == "other"


def test_guess_producer_channel_header_beats_x_monitor():
    # 📢 + · HH:MM is a channel card even when the name looks like a handle.
    assert guess_producer("📢 @somechannel · 12:05\n正文") == "chatdaily_raw"


# --------------------------------------------------------------------------- #
# normalize_for_embedding

def test_normalize_strips_header_line_and_urls():
    out = normalize_for_embedding("📢 测试频道 · 09:30\n正文内容 https://example.com/a?b=1 结束")
    assert "📢" not in out and "09:30" not in out
    assert "http" not in out
    assert "正文内容" in out and "结束" in out


def test_normalize_markdown_link_keeps_label():
    out = normalize_for_embedding("这篇 [深度长文](https://example.com/post) 值得一读")
    assert "深度长文" in out and "http" not in out


def test_normalize_strips_hhmm_stamp_and_meta_line():
    out = normalize_for_embedding("视频标题很长的一条\n👤 某UP主 · 12万播放\n会议 14:30 开始，内容如下")
    assert "👤" not in out and "14:30" not in out
    assert "会议" in out and "视频标题很长的一条" in out


def test_normalize_collapses_whitespace_and_caps_length():
    out = normalize_for_embedding("多行\n\n\n  文本   带空格" + "字" * 3000)
    assert "\n" not in out and "  " not in out
    assert len(out) <= 1500


def test_normalize_empty_inputs():
    assert normalize_for_embedding("") == ""
    assert normalize_for_embedding(None) == ""
    assert normalize_for_embedding("📢 只有标题 · 09:30") == ""


# --------------------------------------------------------------------------- #
# cosine

def test_cosine_basics():
    assert cosine([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    assert cosine([1.0, 0.0], _unit(0.87)) == pytest.approx(0.87)
    assert cosine([], [1.0]) == 0.0
    assert cosine([1.0], [1.0, 0.0]) == 0.0
    assert cosine(None, [1.0]) == 0.0


# --------------------------------------------------------------------------- #
# DeliveredIndex — ingest

def _make_messages_db(path: Path, rows) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE messages (
            chat_id INTEGER NOT NULL,
            chat_name TEXT,
            msg_id INTEGER NOT NULL,
            sender_id INTEGER,
            sender_name TEXT,
            content TEXT,
            timestamp TEXT NOT NULL,
            raw_json TEXT
        )
        """
    )
    conn.executemany("INSERT INTO messages VALUES (?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()


FORUM_BARE = 4424841223
# 相对当前时间：固定日期会在滑出 recent() 的 48h 窗口后让测试过期失败
TS = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()


def _forum_rows():
    return [
        (FORUM_BARE, "forum", 11, 1, "bot",
         "📢 测试频道 · 09:30\n今天发布了一个新的开源项目，支持多模态输入。", TS, None),
        (FORUM_BARE, "forum", 12, 1, "bot",
         "📢 @someone\n刚刚发了一条推文说要开源全部权重，值得关注。", TS, None),
        (FORUM_BARE, "forum", 13, 1, "bot", "", TS, None),  # media-only
        (99, "other-chat", 14, 1, "bot", "别的群的消息，不该被吸收。", TS, None),
    ]


def _write_sent_caption_snapshot(tmp_path, text, mid=4242):
    row = {
        "schema": "sent-content.v1",
        "delivery_state": "confirmed",
        "producer": "x_monitor",
        "chat_id": -1004424841223,
        "message_id": mid,
        "thread_id": 19,
        "content": text,
        "content_hash": hashlib.sha256(text.encode()).hexdigest(),
        "sent_at": datetime.now(timezone.utc).isoformat(),
    }
    snapshot = tmp_path / "xmonitor-snapshot.json"
    write_snapshot((json.dumps(row, ensure_ascii=False) + "\n").encode(),
                   snapshot, source="fixture")
    return snapshot


@pytest.mark.parametrize("mirror_body", [
    "模型发布包含价格和使用范围等完整信息。",
    "镜像正文包含另一段不同的完整内容，也不能覆盖本地向量。",
])
def test_sent_caption_import_preserves_existing_tg_vector(tmp_path, mirror_body):
    old_text = "📢 @dotey[\u200b](https://example.test/post)\n模型发布包含价格和使用范围等完整信息。"
    mirror_text = "📢 @dotey\u200b\n" + mirror_body
    db = tmp_path / "messages.db"
    _make_messages_db(db, [(FORUM_BARE, "forum", 4242, 1, "bot", old_text, TS, None)])
    idx = _index(tmp_path)
    idx.ingest_new(db, -1004424841223, do_sync=False)
    assert idx.backfill_embeddings(FakeEmbedder()) == 1
    before = dict(idx._conn.execute(
        "SELECT * FROM delivered WHERE msg_id=4242"
    ).fetchone())
    assert before["embedding"] is not None
    snapshot = _write_sent_caption_snapshot(tmp_path, mirror_text)

    for _ in range(2):
        assert idx.ingest_sent_ledger(snapshot, -1004424841223) == 0
        after = dict(idx._conn.execute(
            "SELECT * FROM delivered WHERE msg_id=4242"
        ).fetchone())
        assert after["mirror_valid_until"]
        assert {key: value for key, value in after.items() if key != "mirror_valid_until"} == {
            key: value for key, value in before.items() if key != "mirror_valid_until"
        }
        assert after["mirror_source"] is None
        assert [message.msg_id for message in idx.recent()] == [4242]

    payload = json.loads(snapshot.read_text())
    payload["fetched_at"] = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
    snapshot.write_text(json.dumps(payload))
    assert idx.ingest_sent_ledger(snapshot, -1004424841223) == 0
    assert [message.msg_id for message in idx.recent()] == [4242]
    assert idx.coverage(window_hours=48).ratio == 1.0
    assert idx._get_hwm() == 4242


def test_sent_caption_import_preserves_mirror_vector_for_same_normalized_content(tmp_path):
    old_text = "📢 @dotey[\u200b](https://example.test/post)\n模型发布包含价格和使用范围等完整信息。"
    mirror_text = "📢 @dotey\u200b\n模型发布包含价格和使用范围等完整信息。"
    assert old_text != mirror_text
    assert normalize_for_embedding(old_text) == normalize_for_embedding(mirror_text)
    snapshot = _write_sent_caption_snapshot(tmp_path, mirror_text)
    idx = _index(tmp_path)
    idx.register_sent([4242], old_text, "x_monitor", vector=[0.6, 0.8])
    with idx._conn:
        idx._conn.execute(
            "UPDATE delivered SET mirror_source=?,mirror_valid_until=? WHERE msg_id=4242",
            (str(snapshot.resolve()), "2000-01-01T00:00:00+00:00"),
        )
    before = dict(idx._conn.execute("SELECT * FROM delivered WHERE msg_id=4242").fetchone())

    assert idx.ingest_sent_ledger(snapshot, -1004424841223) == 0

    after = dict(idx._conn.execute("SELECT * FROM delivered WHERE msg_id=4242").fetchone())
    assert after["mirror_valid_until"] != before["mirror_valid_until"]
    assert {key: value for key, value in after.items() if key != "mirror_valid_until"} == {
        key: value for key, value in before.items() if key != "mirror_valid_until"
    }
    assert idx.recent()[0].vector == pytest.approx([0.6, 0.8])


@pytest.mark.parametrize("existing_mirror", [False, True])
def test_sent_caption_import_replaces_unembedded_or_changed_mirror_content(tmp_path, existing_mirror):
    snapshot = _write_sent_caption_snapshot(tmp_path, DELIVERED_TEXT)
    idx = _index(tmp_path)
    idx.register_sent([4242], NEW_TEXT, "x_monitor",
                      vector=[0.6, 0.8] if existing_mirror else None)
    if existing_mirror:
        with idx._conn:
            idx._conn.execute("UPDATE delivered SET mirror_source=? WHERE msg_id=4242",
                              (str(snapshot.resolve()),))

    assert idx.ingest_sent_ledger(snapshot, -1004424841223) == 1

    after = idx._conn.execute("SELECT * FROM delivered WHERE msg_id=4242").fetchone()
    assert after["text"] == DELIVERED_TEXT
    assert after["norm_text"] == normalize_for_embedding(DELIVERED_TEXT)
    assert after["embedding"] is None
    assert all(after[name] is None for name, _ in GENERATION_METADATA_COLUMNS)
    assert after["mirror_source"] == str(snapshot.resolve())
    assert idx.backfill_embeddings(FakeEmbedder()) == 1


@pytest.mark.parametrize("vector", [None, [0.6, 0.8]])
def test_sent_caption_import_does_not_modify_other_producers(tmp_path, vector):
    snapshot = _write_sent_caption_snapshot(tmp_path, DELIVERED_TEXT)
    idx = _index(tmp_path)
    idx.register_sent([4242], NEW_TEXT, "chatdaily_raw", vector=vector)
    before = dict(idx._conn.execute("SELECT * FROM delivered WHERE msg_id=4242").fetchone())

    assert idx.ingest_sent_ledger(snapshot, -1004424841223) == 0

    after = dict(idx._conn.execute("SELECT * FROM delivered WHERE msg_id=4242").fetchone())
    assert after == before


def test_ingest_new_reads_rows_advances_hwm_and_is_idempotent(tmp_path, monkeypatch):
    db = tmp_path / "messages.db"
    _make_messages_db(db, _forum_rows())
    synced: list[tuple[str, int]] = []
    monkeypatch.setattr(topic_dedup, "sync_chat",
                        lambda chat_id, limit: synced.append((chat_id, limit)))

    idx = _index(tmp_path)
    inserted = idx.ingest_new(db, "-1004424841223", sync_limit=42)
    assert inserted == 2  # msg 13 is empty, msg 14 is another chat
    assert synced == [("-1004424841223", 42)]
    # hwm advanced past the empty row too — it was ingested, just not stored.
    assert idx._get_hwm() == 13

    # idempotent re-ingest: nothing above the mark.
    assert idx.ingest_new(db, "-1004424841223") == 0

    # producer attribution happened at ingest time.
    rows = {r["msg_id"]: r["producer"] for r in
            idx._conn.execute("SELECT msg_id, producer FROM delivered")}
    assert rows == {11: "chatdaily_raw", 12: "x_monitor"}


def test_ingest_new_accepts_bare_positive_chat_id(tmp_path, monkeypatch):
    db = tmp_path / "messages.db"
    _make_messages_db(db, _forum_rows())
    monkeypatch.setattr(topic_dedup, "sync_chat", lambda chat_id, limit: None)
    idx = _index(tmp_path)
    assert idx.ingest_new(db, str(FORUM_BARE)) == 2


def test_ingest_new_survives_sync_failure(tmp_path, monkeypatch):
    db = tmp_path / "messages.db"
    _make_messages_db(db, _forum_rows())

    def boom(chat_id, limit):
        raise RuntimeError("tg binary missing")

    monkeypatch.setattr(topic_dedup, "sync_chat", boom)
    idx = _index(tmp_path)
    assert idx.ingest_new(db, "-1004424841223") == 2  # existing rows still read


def test_ingest_new_do_sync_false_skips_sync(tmp_path, monkeypatch):
    db = tmp_path / "messages.db"
    _make_messages_db(db, _forum_rows())
    monkeypatch.setattr(topic_dedup, "sync_chat",
                        lambda *a, **kw: pytest.fail("sync must not be called"))
    idx = _index(tmp_path)
    assert idx.ingest_new(db, "-1004424841223", do_sync=False) == 2


def test_ingest_new_fails_open_on_broken_db(tmp_path, monkeypatch):
    monkeypatch.setattr(topic_dedup, "sync_chat", lambda *a, **kw: None)
    idx = _index(tmp_path)
    # connect() creates an empty db without a messages table → caught, 0, no raise
    assert idx.ingest_new(tmp_path / "nope.db", "-1004424841223") == 0
    assert idx.recent() == []  # index still usable


# --------------------------------------------------------------------------- #
# DeliveredIndex — embeddings, register_sent, recent, prune

def test_backfill_embeddings_batches_and_stores_json(tmp_path, monkeypatch):
    db = tmp_path / "messages.db"
    _make_messages_db(db, _forum_rows())
    monkeypatch.setattr(topic_dedup, "sync_chat", lambda *a, **kw: None)
    idx = _index(tmp_path)
    idx.ingest_new(db, "-1004424841223")

    emb = FakeEmbedder()
    assert idx.backfill_embeddings(emb, cap=200) == 2
    got = idx.recent(window_hours=48)
    assert {m.msg_id for m in got} == {11, 12}
    assert all(m.vector == [1.0, 0.0] for m in got)
    # second backfill: nothing left NULL
    assert idx.backfill_embeddings(emb) == 0


def test_existing_seven_column_schema_migrates_without_claiming_legacy_vector(tmp_path):
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE delivered (
            msg_id INTEGER PRIMARY KEY, ts TEXT NOT NULL, producer TEXT NOT NULL,
            thread_id INTEGER, text TEXT NOT NULL, norm_text TEXT NOT NULL,
            embedding TEXT
        );
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
    """)
    conn.execute(
        "INSERT INTO delivered VALUES (?,?,?,?,?,?,?)",
        (1, TS, "chatdaily_raw", None, DELIVERED_TEXT,
         normalize_for_embedding(DELIVERED_TEXT), json.dumps([1.0, 0.0])),
    )
    conn.commit()
    conn.close()

    idx = DeliveredIndex(path, generation=TEST_GENERATION, prune_on_open=False)
    columns = {row[1] for row in idx._conn.execute("PRAGMA table_info(delivered)")}
    metadata_names = [name for name, _ in GENERATION_METADATA_COLUMNS]
    row = idx._conn.execute(
        "SELECT " + ",".join(metadata_names) + " FROM delivered WHERE msg_id=1"
    ).fetchone()

    assert set(metadata_names) <= columns
    assert tuple(row) == (None,) * len(metadata_names)
    assert idx.recent() == []


def test_backfill_rewrites_incompatible_nonnull_vector_with_current_metadata(tmp_path):
    idx = _index(tmp_path)
    with idx._conn:
        idx._conn.execute(
            "INSERT INTO delivered(msg_id,ts,producer,thread_id,text,norm_text,embedding,"
            "generation_id,model_id,dimension) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (1, TS, "chatdaily_raw", None, DELIVERED_TEXT,
             normalize_for_embedding(DELIVERED_TEXT), encode_vector([1.0, 0.0]),
             "old-generation", "old-model", 2),
        )

    assert idx.backfill_embeddings(FakeEmbedder(), cap=1) == 1
    row = idx._conn.execute(
        "SELECT generation_id,model_id,dimension FROM delivered WHERE msg_id=1"
    ).fetchone()
    assert tuple(row) == (
        TEST_GENERATION.generation_id,
        TEST_GENERATION.model_id,
        TEST_GENERATION.dimension,
    )


def test_backfill_embeddings_failure_leaves_rows_null(tmp_path, monkeypatch):
    db = tmp_path / "messages.db"
    _make_messages_db(db, _forum_rows())
    monkeypatch.setattr(topic_dedup, "sync_chat", lambda *a, **kw: None)
    idx = _index(tmp_path)
    idx.ingest_new(db, "-1004424841223")

    emb = FakeEmbedder()
    emb.fail = True
    assert idx.backfill_embeddings(emb) == 0
    assert idx.recent() == []  # still no embedded rows
    emb.fail = False
    assert idx.backfill_embeddings(emb) == 2  # retried next run


def test_register_sent_recent_roundtrip_with_album_ids(tmp_path):
    idx = _index(tmp_path)
    idx.register_sent([200, 201], DELIVERED_TEXT, "chatdaily_raw",
                      thread_id=41, vector=[0.6, 0.8])
    got = idx.recent(window_hours=48)
    assert {m.msg_id for m in got} == {200, 201}  # every album member written
    m = got[0]
    assert m.producer == "chatdaily_raw"
    assert m.vector == pytest.approx([0.6, 0.8])  # float32 storage
    assert m.text == DELIVERED_TEXT
    assert m.norm_text == normalize_for_embedding(DELIVERED_TEXT)


def test_register_sent_noop_and_no_vector_rows_stay_out_of_recent(tmp_path):
    idx = _index(tmp_path)
    idx.register_sent([], "文本", "chatdaily_raw")
    idx.register_sent(None, "文本", "chatdaily_raw")
    idx.register_sent([300], DELIVERED_TEXT, "chatdaily_raw")  # no vector
    assert idx.recent() == []  # only embedded rows participate


def test_register_sent_invalid_vector_keeps_fact_row_unembedded(tmp_path):
    idx = _index(tmp_path)
    idx.register_sent([301], DELIVERED_TEXT, "chatdaily_raw", vector=[float("nan"), 0.0])
    row = idx._conn.execute(
        "SELECT text,embedding,generation_id,model_id,dimension FROM delivered WHERE msg_id=301"
    ).fetchone()
    assert row["text"] == DELIVERED_TEXT
    assert tuple(row)[1:] == (None, None, None, None)


def test_recent_excludes_producers(tmp_path):
    idx = _index(tmp_path)
    idx.register_sent([1], DELIVERED_TEXT, "chatdaily_raw", None, [1.0, 0.0])
    idx.register_sent([2], "⚠️ 某任务连续失败，需要人工介入处理一下。", "alert", None, [1.0, 0.0])
    got = idx.recent(exclude_producers=frozenset({"alert"}))
    assert [m.msg_id for m in got] == [1]


def test_prune_drops_rows_outside_window(tmp_path):
    idx = _index(tmp_path)
    idx.register_sent([1], DELIVERED_TEXT, "chatdaily_raw", None, [1.0, 0.0])
    with idx._conn:
        idx._conn.execute(
            "INSERT INTO delivered(msg_id,ts,producer,thread_id,text,norm_text,embedding) "
            "VALUES (?,?,?,?,?,?,?)",
            (2, "2020-01-01T00:00:00+00:00", "other", None, "旧内容", "旧内容",
             json.dumps([1.0, 0.0])),
        )
    idx.prune(window_days=14)
    ids = [r["msg_id"] for r in idx._conn.execute("SELECT msg_id FROM delivered")]
    assert ids == [1]


def test_register_sent_stores_float32_blob_readable_by_recent(tmp_path):
    idx = _index(tmp_path)
    idx.register_sent([1], DELIVERED_TEXT, "chatdaily_raw", None, [0.6, -0.8])
    raw = idx._conn.execute(
        "SELECT embedding FROM delivered WHERE msg_id=1").fetchone()["embedding"]
    assert isinstance(raw, bytes)
    got = idx.recent(window_hours=48)
    assert got[0].vector == pytest.approx([0.6, -0.8])


def test_backfill_embeddings_writes_float32_blob(tmp_path, monkeypatch):
    db = tmp_path / "messages.db"
    _make_messages_db(db, _forum_rows())
    monkeypatch.setattr(topic_dedup, "sync_chat", lambda *a, **kw: None)
    idx = _index(tmp_path)
    idx.ingest_new(db, "-1004424841223")
    assert idx.backfill_embeddings(FakeEmbedder()) == 2
    types = {r[0] for r in idx._conn.execute(
        "SELECT typeof(embedding) FROM delivered WHERE embedding IS NOT NULL")}
    assert types == {"blob"}


def test_recent_excludes_legacy_json_row_without_generation_metadata(tmp_path):
    idx = _index(tmp_path)
    idx.register_sent([1], DELIVERED_TEXT, "chatdaily_raw", None, [1.0, 0.0])
    with idx._conn:
        idx._conn.execute(
            "INSERT INTO delivered(msg_id,ts,producer,thread_id,text,norm_text,embedding) "
            "VALUES (?,?,?,?,?,?,?)",
            (2, TS, "chatdaily_raw", None, "旧格式行内容够长可以参与检索比较",
             "旧格式行内容够长可以参与检索比较", json.dumps([1.0, 0.0])),
        )
    got = {m.msg_id: m.vector for m in idx.recent(window_hours=48)}
    assert got == {1: [1.0, 0.0]}


def test_recent_excludes_wrong_generation_and_invalid_current_vector(tmp_path):
    idx = _index(tmp_path)
    idx.register_sent([1], DELIVERED_TEXT, "chatdaily_raw", vector=[1.0, 0.0])
    with idx._conn:
        idx._conn.executemany(
            "INSERT INTO delivered(msg_id,ts,producer,thread_id,text,norm_text,embedding,"
            "generation_id,model_id,dimension) VALUES (?,?,?,?,?,?,?,?,?,?)",
            [
                (2, TS, "chatdaily_raw", None, "wrong gen", "wrong gen",
                 encode_vector([1.0, 0.0]), "other-generation",
                 TEST_GENERATION.model_id, 2),
                (3, TS, "chatdaily_raw", None, "nonfinite", "nonfinite",
                 encode_vector([float("nan"), 0.0]), TEST_GENERATION.generation_id,
                 TEST_GENERATION.model_id, 2),
            ],
        )
    assert [row.msg_id for row in idx.recent()] == [1]


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("model_revision", "other-weights"),
        ("query_template", "query-v2"),
        ("document_template", "document-v2"),
        ("payload_version", "text-v2"),
        ("normalization_version", "chatdaily-normalization-v2"),
        ("chunker_version", "chatdaily-chunker-v2"),
        ("context_hash", "f" * 64),
    ],
)
def test_recent_and_coverage_reject_same_generation_id_with_wrong_context(
    tmp_path, column, value
):
    idx = _index(tmp_path, f"context-{column}.db")
    idx.register_sent([1], DELIVERED_TEXT, "chatdaily_raw", vector=[1.0, 0.0])
    with idx._conn:
        idx._conn.execute(f"UPDATE delivered SET {column}=? WHERE msg_id=1", (value,))

    assert idx.recent() == []
    coverage = idx.coverage(window_hours=48)
    assert coverage.valid_rows == 0
    assert coverage.incompatible_rows == 1


def test_coverage_exact_995_boundary(tmp_path):
    idx = _index(tmp_path)
    rows = []
    for msg_id in range(1, 201):
        valid = msg_id <= 199
        rows.append(
            (
                msg_id, TS, "chatdaily_raw", None, f"text {msg_id}", f"text {msg_id}",
                encode_vector([1.0, 0.0]) if valid else None,
                *(generation_metadata_values(TEST_GENERATION)
                  if valid else (None,) * len(GENERATION_METADATA_COLUMNS)),
            )
        )
    with idx._conn:
        idx._conn.executemany(
            "INSERT INTO delivered(msg_id,ts,producer,thread_id,text,norm_text,embedding,"
            + ",".join(name for name, _ in GENERATION_METADATA_COLUMNS)
            + ") VALUES ("
            + ",".join("?" for _ in range(7 + len(GENERATION_METADATA_COLUMNS)))
            + ")",
            rows,
        )
    coverage = idx.coverage(window_hours=48)
    assert coverage.valid_rows == 199
    assert coverage.eligible_rows == 200
    assert coverage.ratio == pytest.approx(0.995)


def test_prune_runs_on_open(tmp_path):
    path = tmp_path / "idx.db"
    idx = DeliveredIndex(path)
    with idx._conn:
        idx._conn.execute(
            "INSERT INTO delivered(msg_id,ts,producer,thread_id,text,norm_text,embedding) "
            "VALUES (?,?,?,?,?,?,?)",
            (7, "2020-01-01T00:00:00+00:00", "other", None, "旧", "旧", None),
        )
    idx.close()
    idx2 = DeliveredIndex(path)
    assert idx2._conn.execute("SELECT COUNT(*) FROM delivered").fetchone()[0] == 0


# --------------------------------------------------------------------------- #
# SameEventJudge parsing

def _matches():
    return [IndexedMsg(msg_id=100, ts="2026-07-15T02:00:00+00:00",
                       producer="chatdaily_raw", text=DELIVERED_TEXT,
                       norm_text=normalize_for_embedding(DELIVERED_TEXT),
                       vector=None)]


def test_judge_parses_valid_fenced_json():
    llm = FakeLLM('```json\n{"same_event": true, "new_info": "minor", "reason": "补充细节"}\n```')
    v = SameEventJudge(llm).judge(NEW_TEXT, _matches())
    assert v.ok and v.same_event and v.new_info == "minor" and v.reason == "补充细节"
    # prompt carries the new card, the matched text, producer and an age line
    assert NEW_TEXT[:50] in llm.prompts[0]
    assert DELIVERED_TEXT[:50] in llm.prompts[0]
    assert "chatdaily_raw" in llm.prompts[0]
    assert "小时前" in llm.prompts[0]


def test_judge_parses_json_with_prose_around_it():
    llm = FakeLLM('我认为不是同一事件。\n{"same_event": false, "new_info": "substantial", '
                  '"reason": "不同公司"}\n以上。')
    v = SameEventJudge(llm).judge(NEW_TEXT, _matches())
    assert v.ok and not v.same_event and v.new_info == "substantial"


def test_judge_parses_fenced_json_with_prose_around_fence():
    llm = FakeLLM('结论如下：\n```json\n{"same_event": true, "new_info": "none", '
                  '"reason": "纯复读"}\n```\n完毕。')
    v = SameEventJudge(llm).judge(NEW_TEXT, _matches())
    assert v.ok and v.same_event and v.new_info == "none"


def test_judge_malformed_output_fails_open():
    v = SameEventJudge(FakeLLM("我觉得是同一件事，但我拒绝输出 JSON。")).judge(NEW_TEXT, _matches())
    assert not v.ok
    assert v.new_info == "substantial"  # fail-open = deliver
    assert not v.same_event


def test_judge_out_of_enum_new_info_coerced_to_substantial():
    llm = FakeLLM('{"same_event": true, "new_info": "huge", "reason": "x"}')
    v = SameEventJudge(llm).judge(NEW_TEXT, _matches())
    assert v.ok and v.same_event and v.new_info == "substantial"


def test_judge_boolish_string_same_event_coerced():
    for raw in ('"yes"', '"true"', '"是"'):
        llm = FakeLLM(f'{{"same_event": {raw}, "new_info": "none", "reason": "x"}}')
        assert SameEventJudge(llm).judge(NEW_TEXT, _matches()).same_event is True
    llm = FakeLLM('{"same_event": "no", "new_info": "none", "reason": "x"}')
    assert SameEventJudge(llm).judge(NEW_TEXT, _matches()).same_event is False


def test_judge_llm_exception_fails_open():
    v = SameEventJudge(FakeLLM(RuntimeError("timeout"))).judge(NEW_TEXT, _matches())
    assert not v.ok and v.new_info == "substantial" and not v.same_event


def test_judge_constructor_overrides_do_not_mutate_shared_client():
    from chat_daily_tg.llm_client import LLMClient
    shared = LLMClient(endpoint="http://127.0.0.1:1", model="orig", api_key="k")
    judge = SameEventJudge(shared, model="judge-model", timeout=30.0, max_tokens=512)
    assert shared.model == "orig" and shared.timeout == 300.0
    assert judge.llm.model == "judge-model"
    assert judge.llm.timeout == 30.0 and judge.llm.max_tokens == 512


# --------------------------------------------------------------------------- #
# TopicDedupGate routing


def _rerank_gate(tmp_path, reranker):
    idx = _index(tmp_path, "rerank-gate.db")
    for msg_id, text, similarity in [
        (101, "dense candidate one long enough for semantic matching", 0.99),
        (102, "dense candidate two long enough for semantic matching", 0.95),
        (103, "dense candidate three long enough for semantic matching", 0.90),
    ]:
        idx.register_sent(
            [msg_id], text, "chatdaily_raw", vector=_unit(similarity)
        )
    embedder = FakeEmbedder({normalize_for_embedding(NEW_TEXT): [1.0, 0.0]})
    judge = FakeJudge(JudgeVerdict(False, "substantial", "different", True))
    gate = TopicDedupGate(
        idx,
        embedder,
        judge,
        reranker=reranker,
        rerank_top_k=3,
        mode="report",
        candidate_min_sim=0.8,
        calibrated_generation_id=TEST_GENERATION.generation_id,
        online_backfill_cap=0,
    )
    return gate, judge


def test_l2_reranker_only_reorders_dense_top3_before_judge(tmp_path):
    class ReverseReranker:
        def __init__(self):
            self.calls = []

        def rerank(self, query, documents, *, top_n):
            self.calls.append((query, documents, top_n))
            return [RerankResult(2, 0.9), RerankResult(1, 0.8), RerankResult(0, 0.7)]

    reranker = ReverseReranker()
    gate, judge = _rerank_gate(tmp_path, reranker)

    verdict = gate.assess(NEW_TEXT)

    assert verdict.action == "deliver" and verdict.judged
    assert [message.msg_id for message in judge.calls[0][1]] == [103, 102, 101]
    assert reranker.calls[0][0] == normalize_for_embedding(NEW_TEXT)
    assert reranker.calls[0][2] == 3


@pytest.mark.parametrize("behavior", ["partial", "raise"])
def test_l2_reranker_partial_or_failure_preserves_dense_order(tmp_path, behavior):
    class BrokenReranker:
        def rerank(self, query, documents, *, top_n):
            if behavior == "raise":
                raise RuntimeError("offline")
            return [RerankResult(2, 0.9)]

    gate, judge = _rerank_gate(tmp_path, BrokenReranker())

    gate.assess(NEW_TEXT)

    assert [message.msg_id for message in judge.calls[0][1]] == [101, 102, 103]


def test_l2_reranker_failure_downgrades_enforce_to_report(tmp_path):
    class BrokenReranker:
        def rerank(self, query, documents, *, top_n):
            raise RuntimeError("offline")

    judge = FakeJudge(JudgeVerdict(True, "none", "same", True))
    gate = _gate(
        tmp_path,
        judge=judge,
        sim=0.99,
        mode="enforce",
        reranker=BrokenReranker(),
    )

    verdict = gate.assess(NEW_TEXT)

    assert gate.effective_mode == "report"
    assert gate.mode_downgrade_reason == "reranker_unavailable"
    assert verdict.action == "deliver"


def test_l2_reranker_default_is_not_invoked(tmp_path):
    gate, judge = _rerank_gate(tmp_path, None)
    gate.assess(NEW_TEXT)
    assert [message.msg_id for message in judge.calls[0][1]] == [101, 102, 103]

def test_gate_below_band_delivers_without_judge(tmp_path, journal):
    judge = FakeJudge(JudgeVerdict(True, "none", "x", True))
    gate = _gate(tmp_path, judge=judge, sim=0.5)
    v = gate.assess(NEW_TEXT)
    assert v.action == "deliver" and v.reason == "no-match"
    assert judge.calls == [] and journal == []
    assert v.vector is not None  # caller can still register_sent with it


def test_gate_in_band_calls_judge(tmp_path):
    judge = FakeJudge(JudgeVerdict(True, "substantial", "新视角", True))
    gate = _gate(tmp_path, judge=judge, sim=0.9, mode="enforce")
    v = gate.assess(NEW_TEXT)
    assert len(judge.calls) == 1
    assert judge.calls[0][1][0].msg_id == 100  # top match handed to the judge
    assert v.judged and v.action == "deliver" and v.reason == "judge-substantial"


def test_gate_none_enforce_skips_and_journals(tmp_path, journal):
    judge = FakeJudge(JudgeVerdict(True, "none", "纯复读", True))
    gate = _gate(tmp_path, judge=judge, sim=0.9, mode="enforce")
    v = gate.assess(NEW_TEXT)
    assert v.action == "skip" and v.matched_msg_id == 100
    assert len(journal) == 1
    entry = journal[0]
    assert entry["layer"] == "L2" and entry["action"] == "skip"
    assert entry["mode"] == "enforce" and entry["new_info"] == "none"


def test_gate_none_annotate_annotates(tmp_path, journal):
    judge = FakeJudge(JudgeVerdict(True, "none", "纯复读", True))
    gate = _gate(tmp_path, judge=judge, sim=0.9, mode="annotate")
    v = gate.assess(NEW_TEXT)
    assert v.action == "annotate" and v.matched_msg_id == 100
    assert journal[0]["action"] == "annotate"


def test_gate_none_report_delivers_but_journals_would_be_skip(tmp_path, journal):
    judge = FakeJudge(JudgeVerdict(True, "none", "纯复读", True))
    gate = _gate(tmp_path, judge=judge, sim=0.9, mode="report")
    v = gate.assess(NEW_TEXT)
    assert v.action == "deliver"  # report mode never withholds
    assert len(journal) == 1
    assert journal[0]["action"] == "skip"      # the would-be action
    assert journal[0]["returned"] == "deliver"
    assert journal[0]["mode"] == "report"


def test_jev_shadow_cannot_change_gate_or_journal(tmp_path, journal):
    judge = FakeJudge(JudgeVerdict(True, 'none', 'duplicate', True))
    gate = _gate(tmp_path, judge=judge, mode='report')
    baseline = gate.assess(NEW_TEXT)
    old_journal = list(journal)
    journal.clear()
    class BrokenShadow:
        def observe(self, *args):
            raise RuntimeError('shadow failed')
    gate.jev_shadow = BrokenShadow()
    shadow_verdict = gate.assess(NEW_TEXT)
    assert shadow_verdict == baseline
    assert len(journal) == 1 and journal[0]['action'] == old_journal[0]['action']


def test_offline_gate_never_calls_jev(tmp_path, journal):
    class Shadow:
        def observe(self, *args):
            pytest.fail('offline gate must not call Jev')
    gate = _gate(tmp_path, mode='report', jev_shadow=Shadow())
    gate.embedder.fail = True
    gate.prepare([NEW_TEXT])
    assert gate.assess(NEW_TEXT).action == 'deliver'
    assert journal == []


def test_gate_minor_annotates_in_enforce(tmp_path, journal):
    # Minor additions retain a link; no-information repeats alone may skip.
    judge = FakeJudge(JudgeVerdict(True, "minor", "补充细节", True))
    gate = _gate(tmp_path, judge=judge, sim=0.9, mode="enforce")
    v = gate.assess(NEW_TEXT)
    assert v.action == "annotate" and v.reason == "judge-minor"
    assert journal[0]["action"] == "annotate"
    assert journal[0]["new_info"] == "minor"


def test_gate_minor_annotates_in_annotate_mode(tmp_path, journal):
    # annotate mode still softens skip → annotate (ratchet rung).
    judge = FakeJudge(JudgeVerdict(True, "minor", "补充细节", True))
    gate = _gate(tmp_path, judge=judge, sim=0.9, mode="annotate")
    v = gate.assess(NEW_TEXT)
    assert v.action == "annotate" and v.reason == "judge-minor"
    assert journal[0]["action"] == "annotate"


def test_gate_substantial_preserves_same_event_update(tmp_path, journal):
    judge = FakeJudge(JudgeVerdict(True, "substantial", "新数据", True))
    gate = _gate(tmp_path, judge=judge, sim=0.9, mode="enforce")
    v = gate.assess(NEW_TEXT)
    assert v.action == "deliver" and v.reason == "judge-substantial"
    assert journal == []


def test_gate_not_same_event_delivers(tmp_path, journal):
    judge = FakeJudge(JudgeVerdict(False, "substantial", "不同事件", True))
    gate = _gate(tmp_path, judge=judge, sim=0.95, mode="enforce")
    v = gate.assess(NEW_TEXT)
    assert v.action == "deliver" and v.reason == "judge-not-same"
    assert journal == []


def test_gate_judge_budget_sixth_call_degrades(tmp_path):
    judge = FakeJudge(JudgeVerdict(True, "substantial", "x", True))
    gate = _gate(tmp_path, judge=judge, sim=0.9, mode="enforce")
    verdicts = [gate.assess(NEW_TEXT) for _ in range(6)]
    assert len(judge.calls) == 5  # budget respected
    sixth = verdicts[5]
    assert not sixth.judged
    # degraded rule: 0.9 < strong_sim 0.93 → deliver
    assert sixth.action == "deliver" and sixth.reason == "degraded-below-strong"


def test_gate_degraded_strong_sim_annotates(tmp_path, journal):
    # No judge at all: strong match annotates, nothing is ever skipped unjudged.
    gate = _gate(tmp_path, judge=None, sim=0.95, mode="annotate")
    v = gate.assess(NEW_TEXT)
    assert v.action == "annotate" and v.reason == "degraded-strong-sim"
    assert not v.judged
    assert journal[0]["action"] == "annotate"


def test_gate_prepare_failure_goes_offline_all_deliver(tmp_path, journal):
    gate = _gate(tmp_path, judge=None, sim=0.99)
    gate.embedder.fail = True
    gate.prepare([NEW_TEXT])
    assert gate.offline
    v = gate.assess(NEW_TEXT)
    assert v.action == "deliver" and v.reason == "offline"
    assert journal == []


def test_gate_prepare_batches_one_embed_call(tmp_path):
    gate = _gate(tmp_path, judge=None, sim=0.5)
    gate.prepare([NEW_TEXT, NEW_TEXT, "短文本"])  # dupes and shorts dropped
    assert gate.embedder.query_batches == [[normalize_for_embedding(NEW_TEXT)]]
    gate.assess(NEW_TEXT)  # cached — no second embed call
    assert len(gate.embedder.query_batches) == 1


def test_gate_lazy_embed_failure_goes_offline(tmp_path):
    gate = _gate(tmp_path, judge=None, sim=0.9)
    gate.embedder.fail = True
    v = gate.assess(NEW_TEXT)  # no prepare(); lazy single embed fails
    assert v.action == "deliver" and v.reason == "embed-error"
    assert gate.offline
    assert gate.assess(NEW_TEXT).reason == "offline"  # one failure, then quiet


def test_gate_judge_raising_fails_open_to_degraded_rule(tmp_path):
    judge = FakeJudge(RuntimeError("rogue judge"))
    strong = _gate(tmp_path, judge=judge, sim=0.95, mode="enforce")
    v = strong.assess(NEW_TEXT)
    assert v.action == "annotate" and v.reason == "degraded-strong-sim"
    assert not v.judged

    judge2 = FakeJudge(RuntimeError("rogue judge"))
    weak = _gate(tmp_path, judge=judge2, sim=0.9, mode="enforce")
    assert weak.assess(NEW_TEXT).action == "deliver"


def test_gate_short_text_delivers(tmp_path):
    gate = _gate(tmp_path, judge=None)
    v = gate.assess("太短了")
    assert v.action == "deliver" and v.reason == "short-text"


def test_gate_internal_error_delivers(tmp_path):
    class BrokenIndex:
        generation = TEST_GENERATION

        def backfill_embeddings(self, *args, **kwargs):
            return 0

        def coverage(self, **kwargs):
            from chat_daily_tg.evidence_index import EmbeddingCoverage

            return EmbeddingCoverage(1, 1, 0, 0, 0)

        def recent(self, **kw):
            raise RuntimeError("db exploded")

    emb = FakeEmbedder()
    gate = TopicDedupGate(BrokenIndex(), emb, None, mode="enforce")
    v = gate.assess(NEW_TEXT)
    assert v.action == "deliver" and v.reason == "gate-error"


def test_gate_unknown_mode_coerced_to_report(tmp_path):
    gate = _gate(tmp_path, judge=None, mode="yolo")
    assert gate.mode == "report"


def test_gate_forces_report_when_generation_not_calibrated(tmp_path, journal):
    judge = FakeJudge(JudgeVerdict(True, "none", "same", True))
    gate = _gate(
        tmp_path,
        judge=judge,
        sim=0.99,
        mode="enforce",
        calibrated_generation_id="different-generation",
    )

    verdict = gate.assess(NEW_TEXT)

    assert gate.effective_mode == "report"
    assert gate.mode_downgrade_reason == "generation_not_calibrated"
    assert verdict.action == "deliver"
    assert journal[0]["requested_mode"] == "enforce"
    assert journal[0]["effective_mode"] == "report"


def test_matching_generation_id_without_formal_receipt_cannot_enforce(
    tmp_path, journal
):
    judge = FakeJudge(JudgeVerdict(True, "none", "same", True))
    gate = _gate(
        tmp_path,
        judge=judge,
        sim=0.99,
        mode="enforce",
        calibration_receipt_path=None,
    )

    verdict = gate.assess(NEW_TEXT)

    assert gate.effective_mode == "report"
    assert gate.mode_downgrade_reason == "calibration_receipt_missing"
    assert verdict.action == "deliver"


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"labeled_query_count": 199}, "calibration_evidence_insufficient"),
        ({"labeled_query_count": 200.0}, "calibration_evidence_insufficient"),
        ({"incremental_success_days": "7"}, "calibration_evidence_insufficient"),
        ({"shadow_availability": "0.995"}, "calibration_evidence_insufficient"),
        ({"shadow_completed_at": "2026-08-07T23:59:59+00:00"},
         "calibration_evidence_insufficient"),
        ({"shadow_started_at": "2999-01-01T00:00:00+00:00",
          "shadow_completed_at": "2999-01-08T00:00:00+00:00",
          "issued_at": "2999-01-08T00:00:00+00:00"},
         "calibration_evidence_insufficient"),
        ({"context_hash": "d" * 64}, "calibration_context_mismatch"),
        ({"context_identity": {
            **TEST_GENERATION.context_identity(),
            "normalized": 1,
        }}, "calibration_context_mismatch"),
        ({"reranker_model_id": "other-reranker"}, "calibration_model_mismatch"),
        ({"candidate_min_sim": 0.81}, "calibration_policy_mismatch"),
        ({"candidate_min_sim": "0.80"}, "calibration_policy_mismatch"),
        ({"rerank_top_k": 3.0}, "calibration_policy_mismatch"),
        ({"retrieval_window_hours": 48.0}, "calibration_policy_mismatch"),
    ],
)
def test_formal_receipt_must_bind_full_context_models_policy_and_evidence(
    tmp_path, overrides, reason
):
    judge = FakeJudge(JudgeVerdict(True, "none", "same", True))
    reranker = IdentityReranker()
    receipt = _write_calibration_receipt(
        tmp_path,
        reranker=reranker,
        judge=judge,
        **overrides,
    )
    gate = _gate(
        tmp_path,
        judge=judge,
        sim=0.99,
        mode="enforce",
        reranker=reranker,
        calibration_receipt_path=receipt,
    )

    verdict = gate.assess(NEW_TEXT)

    assert gate.effective_mode == "report"
    assert gate.mode_downgrade_reason == reason
    assert verdict.action == "deliver"


def test_formal_receipt_rejects_nonfinite_json_number(tmp_path):
    receipt = tmp_path / "topic-dedup-calibration-receipt.v1.json"
    receipt.write_text('{"unexpected":NaN}', encoding="utf-8")
    gate = _gate(
        tmp_path,
        judge=FakeJudge(JudgeVerdict(True, "none", "same", True)),
        sim=0.99,
        mode="enforce",
        calibration_receipt_path=receipt,
    )

    verdict = gate.assess(NEW_TEXT)

    assert gate.effective_mode == "report"
    assert gate.mode_downgrade_reason == "calibration_receipt_invalid"
    assert verdict.action == "deliver"


def test_unversioned_embedding_revision_cannot_enforce_even_with_receipt(tmp_path):
    generation = dataclasses.replace(
        TEST_GENERATION,
        generation_id="unversioned-generation",
        model_revision="unversioned",
        context_hash="",
    )
    idx = DeliveredIndex(tmp_path / "unversioned.db", generation=generation)
    idx.register_sent([100], DELIVERED_TEXT, "chatdaily_raw", vector=[1.0, 0.0])
    embedder = FakeEmbedder({normalize_for_embedding(NEW_TEXT): _unit(0.99)})
    embedder.generation = generation
    reranker = IdentityReranker()
    judge = FakeJudge(JudgeVerdict(True, "none", "same", True))
    receipt = _write_calibration_receipt(
        tmp_path,
        generation=generation,
        reranker=reranker,
        judge=judge,
    )
    gate = TopicDedupGate(
        idx,
        embedder,
        judge,
        mode="enforce",
        reranker=reranker,
        calibrated_generation_id=generation.generation_id,
        calibration_receipt_path=receipt,
    )

    verdict = gate.assess(NEW_TEXT)

    assert gate.effective_mode == "report"
    assert gate.mode_downgrade_reason == "model_revision_unversioned"
    assert verdict.action == "deliver"


def test_gate_forces_report_when_reranker_is_unavailable(tmp_path, journal):
    judge = FakeJudge(JudgeVerdict(True, "none", "same", True))
    gate = _gate(
        tmp_path,
        judge=judge,
        sim=0.99,
        mode="enforce",
        reranker=None,
    )

    verdict = gate.assess(NEW_TEXT)

    assert gate.effective_mode == "report"
    assert gate.mode_downgrade_reason == "reranker_unavailable"
    assert verdict.action == "deliver"
    assert journal[0]["requested_mode"] == "enforce"
    assert journal[0]["effective_mode"] == "report"


def test_gate_forces_report_below_coverage_threshold(tmp_path, journal):
    judge = FakeJudge(JudgeVerdict(True, "none", "same", True))
    gate = _gate(
        tmp_path,
        judge=judge,
        sim=0.99,
        mode="enforce",
        online_backfill_cap=0,
    )
    gate.index.register_sent([101], "另一条足够长且尚未生成文档向量的已送达内容。", "chatdaily_raw")

    verdict = gate.assess(NEW_TEXT)

    assert gate.embedding_coverage is not None
    assert gate.embedding_coverage.ratio == pytest.approx(0.5)
    assert gate.effective_mode == "report"
    assert gate.mode_downgrade_reason == "embedding_coverage"
    assert verdict.action == "deliver"
    assert journal[0]["embedding_coverage"] == pytest.approx(0.5)


@pytest.mark.parametrize("problem", ["missing", "incompatible", "invalid"])
def test_gate_forces_report_when_any_coverage_row_is_not_current(
    tmp_path, journal, problem
):
    judge = FakeJudge(JudgeVerdict(True, "none", "same", True))
    gate = _gate(
        tmp_path,
        judge=judge,
        sim=0.99,
        mode="enforce",
    )
    metadata = generation_metadata_values(TEST_GENERATION)
    rows = [
        (
            msg_id,
            TS,
            "chatdaily_raw",
            None,
            f"valid text {msg_id}",
            f"valid text {msg_id}",
            encode_vector([1.0, 0.0]),
            *metadata,
        )
        for msg_id in range(101, 299)
    ]
    problem_metadata = list(metadata)
    problem_vector = encode_vector([1.0, 0.0])
    if problem == "missing":
        problem_vector = None
    elif problem == "incompatible":
        problem_metadata[0] = "other-generation"
    else:
        problem_vector = encode_vector([float("nan"), 0.0])
    rows.append(
        (
            299,
            TS,
            "chatdaily_raw",
            None,
            "problem row",
            "problem row",
            problem_vector,
            *problem_metadata,
        )
    )
    with gate.index._conn:
        gate.index._conn.executemany(
            "INSERT INTO delivered(msg_id,ts,producer,thread_id,text,norm_text,embedding,"
            + ",".join(name for name, _ in GENERATION_METADATA_COLUMNS)
            + ") VALUES ("
            + ",".join("?" for _ in range(7 + len(GENERATION_METADATA_COLUMNS)))
            + ")",
            rows,
        )

    verdict = gate.assess(NEW_TEXT)

    assert gate.embedding_coverage is not None
    assert gate.embedding_coverage.ratio == pytest.approx(0.995)
    assert gate.effective_mode == "report"
    assert gate.mode_downgrade_reason == "embedding_coverage"
    assert verdict.action == "deliver"
    assert journal[0]["effective_mode"] == "report"


def test_gate_uses_document_encoder_when_templates_are_asymmetric(tmp_path):
    generation = dataclasses.replace(
        TEST_GENERATION,
        generation_id="asymmetric-v1",
        symmetric_query_document=False,
        context_hash="",
    )

    class AsymmetricEmbedder(FakeEmbedder):
        def __init__(self):
            super().__init__()
            self.generation = generation
            self.document_batches: list[list[str]] = []

        def embed_documents(self, texts):
            self.document_batches.append(list(texts))
            return [[0.0, 1.0] for _ in texts]

    idx = DeliveredIndex(tmp_path / "asymmetric.db", generation=generation)
    embedder = AsymmetricEmbedder()
    gate = TopicDedupGate(
        idx,
        embedder,
        mode="report",
        calibrated_generation_id=generation.generation_id,
        online_backfill_cap=0,
    )

    gate.register_sent([777], NEW_TEXT, "chatdaily_raw", vector=[1.0, 0.0])

    assert embedder.document_batches == [[normalize_for_embedding(NEW_TEXT)]]
    assert idx.recent()[0].vector == [0.0, 1.0]


def test_gate_verdict_vector_roundtrips_into_register_sent(tmp_path):
    gate = _gate(tmp_path, judge=None, sim=0.5)
    v = gate.assess(NEW_TEXT)
    assert v.action == "deliver"
    gate.index.register_sent([500], NEW_TEXT, "chatdaily_raw", None, v.vector)
    got = [m for m in gate.index.recent() if m.msg_id == 500]
    assert got and got[0].vector == pytest.approx(_unit(0.5))


# --------------------------------------------------------------------------- #
# annotation html

def test_annotation_html_contains_deep_link(tmp_path):
    gate = _gate(tmp_path, judge=None)
    html = gate.annotation_html(5555)
    assert "🔁 疑似同一事件" in html
    assert 'href="https://t.me/c/4424841223/5555"' in html


def test_annotation_html_uses_constructor_group_id(tmp_path):
    gate = _gate(tmp_path, judge=None, group_internal_id="123456")
    assert 'https://t.me/c/123456/9' in gate.annotation_html(9)


# --------------------------------------------------------------------------- #
# regression pins (2026-07-16 review fixes)

def test_gate_register_sent_visible_to_same_run_assess_via_cache(tmp_path, monkeypatch):
    """Same-run collision: once the per-run retrieval cache is warm, a card
    registered through the GATE (not the bare index) must be seen by a later
    assess() in the same run — without any re-read of the index."""
    query_a = "第一条卡片讲的是某公司发布新产品的详细情况，包含定价与上市时间安排。"
    sent_text = "某开源项目发布重大版本更新，带来了全新的插件系统与更快的构建速度。"
    query_b = "近似复读：某开源项目发布重大版本更新，带来了全新的插件系统与更快构建。"
    orth = [0.0, 1.0]  # orthogonal to the pre-existing row's [1, 0]

    idx = _index(tmp_path)
    idx.register_sent([100], DELIVERED_TEXT, "chatdaily_raw", None, [1.0, 0.0])
    emb = FakeEmbedder({
        normalize_for_embedding(query_a): orth,
        normalize_for_embedding(query_b): orth,
    })
    gate = TopicDedupGate(
        idx,
        emb,
        None,
        mode="annotate",
        calibrated_generation_id=TEST_GENERATION.generation_id,
    )

    v1 = gate.assess(query_a)
    assert v1.action == "deliver" and v1.reason == "no-match"  # cache warmed

    # From here on, any re-read of the index is a regression.
    monkeypatch.setattr(
        gate.index, "recent",
        lambda **kw: pytest.fail("per-run cache must not be re-read"))

    gate.register_sent([500], sent_text, "chatdaily_raw", vector=orth)
    v2 = gate.assess(query_b)
    assert v2.matched_msg_id == 500          # same-run collision caught
    assert v2.similarity == pytest.approx(1.0)
    assert v2.action == "annotate" and v2.reason == "degraded-strong-sim"


def test_gate_deferred_ingest_runs_exactly_once_across_prepares(tmp_path):
    """prepare may copy source text once but never performs embedding backfill."""

    class CountingIndex:
        def __init__(self):
            self.ingest_calls = 0
            self.backfill_calls = 0
            self.generation = TEST_GENERATION

        def ingest_new(self, db_path, forum_chat_id, sync_limit=300):
            self.ingest_calls += 1
            return 0

        def backfill_embeddings(self, embedder, cap=200, *, window_hours=None):
            self.backfill_calls += 1
            raise AssertionError("query-time embedding backfill is forbidden")

        def coverage(self, **kw):
            from chat_daily_tg.evidence_index import EmbeddingCoverage

            return EmbeddingCoverage(1, 1, 0, 0, 0)

        def recent(self, **kw):
            return []

    idx = CountingIndex()
    gate = TopicDedupGate(
        idx, FakeEmbedder(), None,
        ingest={"db_path": tmp_path / "messages.db",
                "forum_chat_id": "-1004424841223"},
    )
    gate.prepare([NEW_TEXT])
    gate.prepare([DELIVERED_TEXT])  # new text → passes the norm filter again
    assert idx.ingest_calls == 1
    assert idx.backfill_calls == 0


def test_config_default_exclude_producers_matches_module_constant():
    # config.DedupTopic duplicates the default exclusion list; pin the two so
    # neither can drift without a test failing.
    from chat_daily_tg.config import DedupTopic
    from chat_daily_tg.topic_dedup import DEFAULT_EXCLUDE_PRODUCERS
    assert set(DedupTopic().exclude_producers) == set(DEFAULT_EXCLUDE_PRODUCERS)


def test_legacy_calibration_script_writes_only_exploratory_markdown(tmp_path):
    from scripts import calibrate_topic_dedup

    report = calibrate_topic_dedup.Report()
    report.md_only("historical small-sample result")

    path = calibrate_topic_dedup.write_report(
        report, SimpleNamespace(report_dir=tmp_path)
    )
    text = path.read_text(encoding="utf-8")

    assert path.name.startswith("topic-dedup-exploratory-")
    assert "NON-AUTHORITATIVE / REPORT-ONLY" in text
    assert L2_CALIBRATION_RECEIPT_SCHEMA in text
    assert path.suffix == ".md"


def test_gate_report_journal_includes_ref_identity(tmp_path, journal):
    """The journaled would-be skip must carry the card's own chat_id/msg_id/
    channel — without them the --resend CHAT_ID:MSG_ID escape hatch has nothing
    to drive."""
    judge = FakeJudge(JudgeVerdict(True, "none", "纯复读", True))
    gate = _gate(tmp_path, judge=judge, sim=0.9, mode="report")
    ref = {"chat_id": "-1001833253016", "msg_id": 13927, "channel": "yihong"}
    v = gate.assess(NEW_TEXT, ref=ref)
    assert v.action == "deliver"  # report mode never withholds
    assert len(journal) == 1
    e = journal[0]
    assert e["chat_id"] == "-1001833253016"
    assert e["msg_id"] == 13927
    assert e["channel"] == "yihong"
    assert e["action"] == "skip" and e["returned"] == "deliver"


def test_jev_primary_controls_gate_in_report_mode(tmp_path, journal):
    from chat_daily_tg.jev_client import JevResponse
    from chat_daily_tg.jev_judge import JevJudge

    class Client:
        retry_max_attempts = 1

        def evaluate(self, **kwargs):
            return JevResponse("jev-1.13.0", {
                "same_event": {"probability": 0.99},
                "new_info": {"choice": "none"},
            }, {}, {}, 1)

    judge = JevJudge(Client(), path=tmp_path/"jev.jsonl")
    gate = _gate(tmp_path, judge=judge, mode="report")
    verdict = gate.assess(NEW_TEXT, {"chat_id": "-1001", "msg_id": 77})
    assert verdict.action == "deliver" and verdict.judged
    assert verdict.reason == "judge-none"
    assert journal[-1]["action"] == "skip" and journal[-1]["returned"] == "deliver"
    assert json.loads((tmp_path/"jev.jsonl").read_text())["status"] == "ok"


def test_media_caption_cannot_suppress_distinct_images(tmp_path, journal):
    gate = _gate(tmp_path, mode="enforce", judge=FakeJudge(JudgeVerdict(True, "none", "duplicate", True)))
    verdict = gate.assess(NEW_TEXT, ref={"has_media": True})
    assert verdict.action == "annotate"
    assert journal[-1]["returned"] == "annotate"


def test_terminal_l2_requires_durable_journal(tmp_path, monkeypatch):
    gate = _gate(tmp_path, mode="enforce", judge=FakeJudge(JudgeVerdict(True, "none", "duplicate", True)))
    monkeypatch.setattr(topic_dedup.dedup_journal, "record", lambda entry: False)
    verdict = gate.assess(NEW_TEXT)
    assert verdict.action == "deliver"
    assert verdict.reason == "journal-unavailable"
