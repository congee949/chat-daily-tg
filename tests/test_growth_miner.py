from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from chat_daily_tg import growth_store
from chat_daily_tg.config import Config
from chat_daily_tg.growth_miner import GrowthMiningError, MINER_SYSTEM, mine_day

DATE = "2026-07-11"
CHAT_ID = 1162433032
NAME = "电丸朱氏会社"

# span 1782518..1782522 — anchored on the real msg 1782520 (Beijing 22:23, "A K"),
# with the space kept verbatim. 1782519 is a short 😂 the transcript skips but the
# slice/validation keep.
_STD_ROWS = [
    (CHAT_ID, NAME, 1782518, "A K", "我们来聊聊价值这件事", "2026-07-11T14:20:00+00:00", None),
    (CHAT_ID, NAME, 1782519, "B", "😂", "2026-07-11T14:21:00+00:00", None),
    (CHAT_ID, NAME, 1782520, "A K", "价值是创造出来的 不是节省出来的", "2026-07-11T14:23:00+00:00", None),
    (CHAT_ID, NAME, 1782521, "C", "所以省钱的意义没那么大", "2026-07-11T14:24:00+00:00", None),
    (CHAT_ID, NAME, 1782522, "A K", "对，要把精力放在创造上", "2026-07-11T14:25:00+00:00", None),
]


def _make_db(path: Path, rows: list) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE messages (chat_id INTEGER NOT NULL, chat_name TEXT, "
        "msg_id INTEGER NOT NULL, sender_name TEXT, content TEXT, "
        "timestamp TEXT NOT NULL, raw_json TEXT)"
    )
    conn.executemany("INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
    conn.commit()
    conn.close()


def _make_cfg(db_path: Path, **growth_over) -> Config:
    growth = {"enabled": True, "source": {"id": "-1001162433032", "name": NAME},
              "min_segment_msgs": 3, "max_segment_msgs": 300}
    growth.update(growth_over)
    return Config(
        telegram={"bot_token_env": "TB", "chat_id_env": "TC"},
        llm={"endpoint": "http://x", "model": "m", "api_key_env": "K"},
        sources={"telegram": {"enabled": True, "db_path": str(db_path),
                              "chats": [{"id": "1162433032", "name": NAME}]}},
        growth=growth,
    )


class FakeLLM:
    """Mirrors llm_client.LLMClient.chat: chat(prompt, system=None) -> (str, dict).
    Returns canned responses in order; records every call for assertions."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[tuple[str, str | None]] = []

    def chat(self, prompt: str, system: str | None = None):
        self.calls.append((prompt, system))
        content = self.responses.pop(0) if self.responses else '{"segments": []}'
        return content, {}


def _seg(start: int, end: int, **over) -> dict:
    seg = {"start_msg_id": start, "end_msg_id": end, "theme": "价值创造",
           "points": ["把精力放在创造上"], "quotes": [], "participants": [],
           "score": 8, "reason": "x"}
    seg.update(over)
    return seg


def _llm_json(*segments: dict) -> str:
    return json.dumps({"segments": list(segments)}, ensure_ascii=False)


def _paths(tmp_path: Path):
    return tmp_path / "store.db", tmp_path / "segments"


def test_happy_path_inserts_pending_segment_and_marks_day(tmp_path: Path):
    db = tmp_path / "m.db"
    _make_db(db, _STD_ROWS)
    store, seg_dir = _paths(tmp_path)
    llm = FakeLLM([_llm_json(_seg(
        1782518, 1782522,
        quotes=[{"msg_id": 1782520, "sender": "wrong", "text": "价值是创造出来的 不是节省出来的"}],
    ))])

    inserted, candidates = mine_day(llm, _make_cfg(db), DATE,
                                    store_db=store, segments_dir=seg_dir, messages_db=db)

    assert candidates == 1
    assert len(inserted) == 1
    seg = inserted[0]
    assert seg.status == "pending"
    assert seg.start_hm == "22:20" and seg.end_hm == "22:25"  # Beijing HH:MM of first/last span row
    assert seg.msg_count == 5                                  # span includes the skipped 😂
    assert seg.participants == "A K, B, C"                     # computed from span, by msg count
    assert seg.quotes[0]["sender"] == "A K"                    # overwritten from DB, not "wrong"

    slice_file = seg_dir / "2026" / "07" / "11-1782518.md"
    assert slice_file.exists()
    text = slice_file.read_text(encoding="utf-8")
    for mid in (1782518, 1782519, 1782520, 1782521, 1782522):
        assert f"[{mid}]" in text
    assert "😂" in text                                        # archive keeps the short msg

    assert growth_store.day_already_mined(store, CHAT_ID, DATE)
    assert llm.calls[0][1] == MINER_SYSTEM                     # system prompt passed through


def test_second_mine_day_on_mined_day_is_noop(tmp_path: Path):
    db = tmp_path / "m.db"
    _make_db(db, _STD_ROWS)
    cfg = _make_cfg(db)
    store, seg_dir = _paths(tmp_path)
    first = FakeLLM([_llm_json(_seg(1782518, 1782522,
        quotes=[{"msg_id": 1782520, "sender": "x", "text": "价值是创造出来的 不是节省出来的"}]))])
    mine_day(first, cfg, DATE, store_db=store, segments_dir=seg_dir, messages_db=db)

    second = FakeLLM([_llm_json(_seg(1782518, 1782522))])
    inserted, candidates = mine_day(second, cfg, DATE,
                                    store_db=store, segments_dir=seg_dir, messages_db=db)

    assert inserted == [] and candidates == 0
    assert second.calls == []  # short-circuits before touching the LLM


def test_empty_day_marks_mined_without_llm(tmp_path: Path):
    db = tmp_path / "m.db"
    _make_db(db, [])  # no messages at all
    store, seg_dir = _paths(tmp_path)
    llm = FakeLLM([])

    inserted, candidates = mine_day(llm, _make_cfg(db), DATE,
                                    store_db=store, segments_dir=seg_dir, messages_db=db)

    assert inserted == [] and candidates == 0
    assert llm.calls == []
    assert growth_store.day_already_mined(store, CHAT_ID, DATE)


def test_fenced_json_reply_is_tolerated(tmp_path: Path):
    db = tmp_path / "m.db"
    _make_db(db, _STD_ROWS)
    store, seg_dir = _paths(tmp_path)
    # LLM wraps the JSON in a ```json fence despite instructions — still parses.
    fenced = "```json\n" + _llm_json(_seg(1782518, 1782522)) + "\n```"
    llm = FakeLLM([fenced])

    inserted, candidates = mine_day(llm, _make_cfg(db), DATE,
                                    store_db=store, segments_dir=seg_dir, messages_db=db)

    assert candidates == 1 and len(inserted) == 1
    assert growth_store.day_already_mined(store, CHAT_ID, DATE)


def test_hallucinated_start_dropped_day_still_marked(tmp_path: Path):
    db = tmp_path / "m.db"
    _make_db(db, _STD_ROWS)
    store, seg_dir = _paths(tmp_path)
    llm = FakeLLM([_llm_json(_seg(9999999, 1782522))])  # start_msg_id not in the day

    inserted, candidates = mine_day(llm, _make_cfg(db), DATE,
                                    store_db=store, segments_dir=seg_dir, messages_db=db)

    assert inserted == [] and candidates == 0
    assert len(llm.calls) == 1  # chunk parsed fine — this is a validation drop, not a chunk failure
    assert growth_store.day_already_mined(store, CHAT_ID, DATE)


def test_bad_scores_coerce_to_zero_and_store_rejected(tmp_path: Path):
    rows = [
        (CHAT_ID, NAME, 100, "A", "讨论内容甲一二三", "2026-07-11T02:00:00+00:00", None),
        (CHAT_ID, NAME, 101, "B", "讨论内容甲一二三", "2026-07-11T02:01:00+00:00", None),
        (CHAT_ID, NAME, 102, "A", "讨论内容甲一二三", "2026-07-11T02:02:00+00:00", None),
        (CHAT_ID, NAME, 200, "A", "讨论内容乙一二三", "2026-07-11T03:00:00+00:00", None),
        (CHAT_ID, NAME, 201, "B", "讨论内容乙一二三", "2026-07-11T03:01:00+00:00", None),
        (CHAT_ID, NAME, 202, "A", "讨论内容乙一二三", "2026-07-11T03:02:00+00:00", None),
        (CHAT_ID, NAME, 300, "A", "讨论内容丙一二三", "2026-07-11T04:00:00+00:00", None),
        (CHAT_ID, NAME, 301, "B", "讨论内容丙一二三", "2026-07-11T04:01:00+00:00", None),
        (CHAT_ID, NAME, 302, "A", "讨论内容丙一二三", "2026-07-11T04:02:00+00:00", None),
    ]
    db = tmp_path / "m.db"
    _make_db(db, rows)
    store, seg_dir = _paths(tmp_path)
    llm = FakeLLM([_llm_json(
        _seg(100, 102, score="abc"),
        _seg(200, 202, score=15),
        _seg(300, 302, score=-3),
    )])

    inserted, candidates = mine_day(llm, _make_cfg(db), DATE,
                                    store_db=store, segments_dir=seg_dir, messages_db=db)

    assert candidates == 3
    assert len(inserted) == 3
    assert all(s.status == "rejected" for s in inserted)
    assert all(s.score == 0.0 for s in inserted)
    assert not seg_dir.exists()  # rejected segments get no slice files


def test_whitespace_diff_quote_normalized_to_db_original(tmp_path: Path):
    db = tmp_path / "m.db"
    _make_db(db, _STD_ROWS)
    store, seg_dir = _paths(tmp_path)
    # LLM drops the space that the DB content has — only whitespace differs, so it
    # stays valid, but the STORED text is the DB's own verbatim slice (space back).
    llm = FakeLLM([_llm_json(_seg(1782518, 1782522,
        quotes=[{"msg_id": 1782520, "sender": "q", "text": "价值是创造出来的不是节省出来的"}]))])

    inserted, _ = mine_day(llm, _make_cfg(db), DATE,
                           store_db=store, segments_dir=seg_dir, messages_db=db)

    quote = inserted[0].quotes[0]
    assert quote["text"] == "价值是创造出来的 不是节省出来的"  # DB original, not LLM spacing
    assert quote["sender"] == "A K"                            # sender from DB row
    assert quote["msg_id"] == 1782520


def test_short_quote_dropped_and_quoteless_pending_downgraded(tmp_path: Path):
    db = tmp_path / "m.db"
    _make_db(db, _STD_ROWS)
    store, seg_dir = _paths(tmp_path)
    # One 3-char fragment (< MIN_QUOTE_CHARS) and one 200-char blob (> MAX_QUOTE_CHARS)
    # → both dropped; a pending-score segment with zero surviving verbatim quotes
    # has no trusted anchor → rejected.
    llm = FakeLLM([_llm_json(_seg(1782518, 1782522, score=9,
        quotes=[{"msg_id": 1782520, "sender": "q", "text": "价值是"},
                {"msg_id": 1782520, "sender": "q", "text": "长" * 200}]))])

    inserted, _ = mine_day(llm, _make_cfg(db), DATE,
                           store_db=store, segments_dir=seg_dir, messages_db=db)

    assert len(inserted) == 1
    assert inserted[0].quotes == []
    assert inserted[0].status == "rejected"
    assert not (Path(seg_dir)).exists()  # rejected → no slice file


def test_fabricated_quote_dropped_segment_survives(tmp_path: Path):
    db = tmp_path / "m.db"
    _make_db(db, _STD_ROWS)
    store, seg_dir = _paths(tmp_path)
    llm = FakeLLM([_llm_json(_seg(1782518, 1782522, quotes=[
        {"msg_id": 1782520, "sender": "x", "text": "价值是创造出来的 不是节省出来的"},  # real
        {"msg_id": 1782521, "sender": "y", "text": "这句话在任何消息里都不存在"},         # fabricated
    ]))])

    inserted, _ = mine_day(llm, _make_cfg(db), DATE,
                           store_db=store, segments_dir=seg_dir, messages_db=db)

    quotes = inserted[0].quotes
    assert len(quotes) == 1
    assert quotes[0]["text"] == "价值是创造出来的 不是节省出来的"


def test_quote_with_wrong_msg_id_relocated(tmp_path: Path):
    db = tmp_path / "m.db"
    _make_db(db, _STD_ROWS)
    store, seg_dir = _paths(tmp_path)
    # msg_id 1782521's content doesn't contain the quote, but 1782520's does → relocate.
    llm = FakeLLM([_llm_json(_seg(1782518, 1782522,
        quotes=[{"msg_id": 1782521, "sender": "z", "text": "价值是创造出来的 不是节省出来的"}]))])

    inserted, _ = mine_day(llm, _make_cfg(db), DATE,
                           store_db=store, segments_dir=seg_dir, messages_db=db)

    quote = inserted[0].quotes[0]
    assert quote["msg_id"] == 1782520  # fixed from the wrong 1782521
    assert quote["sender"] == "A K"


def test_span_over_four_hours_dropped(tmp_path: Path):
    rows = [
        (CHAT_ID, NAME, 500, "A", "早上聊起来的话题一二三", "2026-07-11T02:00:00+00:00", None),  # 10:00
        (CHAT_ID, NAME, 501, "B", "接着说了几句一二三", "2026-07-11T02:01:00+00:00", None),      # 10:01
        (CHAT_ID, NAME, 502, "A", "到下午才收尾一二三", "2026-07-11T06:30:00+00:00", None),      # 14:30 → 4h30m
    ]
    db = tmp_path / "m.db"
    _make_db(db, rows)
    store, seg_dir = _paths(tmp_path)
    llm = FakeLLM([_llm_json(_seg(500, 502))])

    inserted, candidates = mine_day(llm, _make_cfg(db), DATE,
                                    store_db=store, segments_dir=seg_dir, messages_db=db)

    assert inserted == [] and candidates == 0
    assert growth_store.day_already_mined(store, CHAT_ID, DATE)  # parsed fine, just dropped


def test_span_below_min_msgs_dropped(tmp_path: Path):
    db = tmp_path / "m.db"
    _make_db(db, _STD_ROWS)
    store, seg_dir = _paths(tmp_path)
    llm = FakeLLM([_llm_json(_seg(1782518, 1782519))])  # span = 2 rows < min_segment_msgs=3

    inserted, candidates = mine_day(llm, _make_cfg(db), DATE,
                                    store_db=store, segments_dir=seg_dir, messages_db=db)

    assert inserted == [] and candidates == 0
    assert growth_store.day_already_mined(store, CHAT_ID, DATE)


def test_chunking_splits_at_ten_minute_gap(tmp_path: Path):
    content = "测试分块逻辑" * 10  # 60 chars → each transcript line comfortably over the tiny budget
    rows = [
        (CHAT_ID, NAME, 1000, "A", content, "2026-07-11T14:00:00+00:00", None),
        (CHAT_ID, NAME, 2000, "B", content, "2026-07-11T14:02:00+00:00", None),
        (CHAT_ID, NAME, 3000, "C", content, "2026-07-11T14:20:00+00:00", None),  # 18-min gap here
        (CHAT_ID, NAME, 4000, "D", content, "2026-07-11T14:22:00+00:00", None),
    ]
    db = tmp_path / "m.db"
    _make_db(db, rows)
    store, seg_dir = _paths(tmp_path)
    llm = FakeLLM([])  # each chunk gets the default {"segments": []}

    mine_day(llm, _make_cfg(db, chunk_chars=120), DATE,
             store_db=store, segments_dir=seg_dir, messages_db=db)

    assert len(llm.calls) == 2
    first_prompt, second_prompt = llm.calls[0][0], llm.calls[1][0]
    assert "[1000]" in first_prompt and "[2000]" in first_prompt
    assert "[3000]" not in first_prompt and "[4000]" not in first_prompt
    assert "[3000]" in second_prompt and "[4000]" in second_prompt
    assert "[1000]" not in second_prompt and "[2000]" not in second_prompt


def test_chunk_parse_failure_raises_but_keeps_good_chunk(tmp_path: Path):
    content = "测试内容消息" * 5  # 30 chars
    rows = [
        (CHAT_ID, NAME, 1000, "A", content, "2026-07-11T14:00:00+00:00", None),
        (CHAT_ID, NAME, 1001, "B", content, "2026-07-11T14:01:00+00:00", None),
        (CHAT_ID, NAME, 1002, "A", content, "2026-07-11T14:02:00+00:00", None),
        (CHAT_ID, NAME, 2000, "C", content, "2026-07-11T14:20:00+00:00", None),  # 18-min gap
        (CHAT_ID, NAME, 2001, "D", content, "2026-07-11T14:21:00+00:00", None),
    ]
    db = tmp_path / "m.db"
    _make_db(db, rows)
    store, seg_dir = _paths(tmp_path)
    # chunk1 (1000-1002) → a valid pending segment; chunk2 (2000-2001) → garbage.
    llm = FakeLLM([_llm_json(_seg(1000, 1002, score=8,
        quotes=[{"msg_id": 1000, "sender": "A", "text": "测试内容消息测试内容"}])),
        "not json at all"])

    with pytest.raises(GrowthMiningError):
        mine_day(llm, _make_cfg(db, chunk_chars=120), DATE,
                 store_db=store, segments_dir=seg_dir, messages_db=db)

    assert len(llm.calls) == 2
    assert growth_store.queue_stats(store)["pending"] == 1        # good chunk's segment inserted
    assert not growth_store.day_already_mined(store, CHAT_ID, DATE)  # day left unmarked for retry


def test_clean_points_bold_markers():
    from chat_daily_tg.growth_miner import _clean_points
    # 纯文本 ≤80 → 原样保留标记（渲染层转 <b>）
    assert _clean_points(["把注意力放在**开源**而非节流"]) == ["把注意力放在**开源**而非节流"]
    # 纯文本超限 → 丢标记按纯文本截断
    assert _clean_points(["**" + "长" * 90 + "**"]) == ["长" * 80]
    # 只有标记没有文字 → 丢弃
    assert _clean_points(["****", "正常要点"]) == ["正常要点"]
