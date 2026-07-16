"""All-offline tests for the L2 topic-level dedup module.

No network, no real tg-cli db, no real journal file: the tg sync is
monkeypatched, embeddings come from a deterministic fake, the LLM judge is a
canned object, and dedup_journal.record is captured by an autouse fixture.
"""
from __future__ import annotations

import json
import math
import sqlite3
from pathlib import Path

import pytest

from chat_daily_tg import topic_dedup
from chat_daily_tg.topic_dedup import (
    DeliveredIndex,
    GateVerdict,
    IndexedMsg,
    JudgeVerdict,
    SameEventJudge,
    TopicDedupGate,
    cosine,
    guess_producer,
    normalize_for_embedding,
)


# --------------------------------------------------------------------------- #
# fixtures & fakes

DELIVERED_TEXT = "某大模型公司今天发布新一代旗舰模型，上下文窗口翻倍，API 价格下调三成，开发者即日可用。"
NEW_TEXT = "刚看到消息：那家大模型公司发布了新旗舰模型，窗口翻倍价格下调，社区反应热烈，值得关注一下。"


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
        self.calls: list[tuple[str, list]] = []

    def judge(self, new_text, matches):
        self.calls.append((new_text, matches))
        if isinstance(self.verdict, Exception):
            raise self.verdict
        return self.verdict


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
    return DeliveredIndex(tmp_path / name)


def _gate(tmp_path, *, mode="annotate", judge=None, sim=0.9, **kw) -> TopicDedupGate:
    """Index with one delivered row at vector [1,0]; new text embeds at `sim`."""
    idx = _index(tmp_path)
    idx.register_sent([100], DELIVERED_TEXT, "chatdaily_raw", None, [1.0, 0.0])
    emb = FakeEmbedder({normalize_for_embedding(NEW_TEXT): _unit(sim)})
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
TS = "2026-07-15T02:00:00+00:00"


def _forum_rows():
    return [
        (FORUM_BARE, "forum", 11, 1, "bot",
         "📢 测试频道 · 09:30\n今天发布了一个新的开源项目，支持多模态输入。", TS, None),
        (FORUM_BARE, "forum", 12, 1, "bot",
         "📢 @someone\n刚刚发了一条推文说要开源全部权重，值得关注。", TS, None),
        (FORUM_BARE, "forum", 13, 1, "bot", "", TS, None),  # media-only
        (99, "other-chat", 14, 1, "bot", "别的群的消息，不该被吸收。", TS, None),
    ]


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
    assert m.vector == [0.6, 0.8]
    assert m.text == DELIVERED_TEXT
    assert m.norm_text == normalize_for_embedding(DELIVERED_TEXT)


def test_register_sent_noop_and_no_vector_rows_stay_out_of_recent(tmp_path):
    idx = _index(tmp_path)
    idx.register_sent([], "文本", "chatdaily_raw")
    idx.register_sent(None, "文本", "chatdaily_raw")
    idx.register_sent([300], DELIVERED_TEXT, "chatdaily_raw")  # no vector
    assert idx.recent() == []  # only embedded rows participate


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
            "INSERT INTO delivered VALUES (?,?,?,?,?,?,?)",
            (2, "2020-01-01T00:00:00+00:00", "other", None, "旧内容", "旧内容",
             json.dumps([1.0, 0.0])),
        )
    idx.prune(window_days=14)
    ids = [r["msg_id"] for r in idx._conn.execute("SELECT msg_id FROM delivered")]
    assert ids == [1]


def test_prune_runs_on_open(tmp_path):
    path = tmp_path / "idx.db"
    idx = DeliveredIndex(path)
    with idx._conn:
        idx._conn.execute(
            "INSERT INTO delivered VALUES (?,?,?,?,?,?,?)",
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

def test_gate_below_band_delivers_without_judge(tmp_path, journal):
    judge = FakeJudge(JudgeVerdict(True, "none", "x", True))
    gate = _gate(tmp_path, judge=judge, sim=0.5)
    v = gate.assess(NEW_TEXT)
    assert v.action == "deliver" and v.reason == "no-match"
    assert judge.calls == [] and journal == []
    assert v.vector is not None  # caller can still register_sent with it


def test_gate_in_band_calls_judge(tmp_path):
    judge = FakeJudge(JudgeVerdict(True, "substantial", "新视角", True))
    gate = _gate(tmp_path, judge=judge, sim=0.9)
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


def test_gate_minor_annotates_in_enforce(tmp_path, journal):
    judge = FakeJudge(JudgeVerdict(True, "minor", "补充细节", True))
    gate = _gate(tmp_path, judge=judge, sim=0.9, mode="enforce")
    v = gate.assess(NEW_TEXT)
    assert v.action == "annotate" and v.reason == "judge-minor"
    assert journal[0]["action"] == "annotate"


def test_gate_substantial_delivers_clean(tmp_path, journal):
    judge = FakeJudge(JudgeVerdict(True, "substantial", "新数据", True))
    gate = _gate(tmp_path, judge=judge, sim=0.9, mode="enforce")
    assert gate.assess(NEW_TEXT).action == "deliver"
    assert journal == []  # clean deliveries are not journaled


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
    gate = _gate(tmp_path, judge=None, sim=0.95, mode="enforce")
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
        def recent(self, **kw):
            raise RuntimeError("db exploded")

    emb = FakeEmbedder()
    gate = TopicDedupGate(BrokenIndex(), emb, None, mode="enforce")
    v = gate.assess(NEW_TEXT)
    assert v.action == "deliver" and v.reason == "gate-error"


def test_gate_unknown_mode_coerced_to_report(tmp_path):
    gate = _gate(tmp_path, judge=None, mode="yolo")
    assert gate.mode == "report"


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
