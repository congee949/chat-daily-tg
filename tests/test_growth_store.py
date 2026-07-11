from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from chat_daily_tg.growth_store import (
    LOCAL_TZ,
    GrowthSegment,
    ab_stats,
    day_already_mined,
    insert_segments,
    latest_ab_pair,
    log_ab,
    mark_day_mined,
    mark_sent,
    mined_days_summary,
    pick_next,
    queue_stats,
    recent_sent,
    segment_id,
    sent_count_on,
    write_slice_file,
)


def _seg(date: str, start: int, end: int, score: float = 7.0,
         status: str = "pending", **kw) -> GrowthSegment:
    return GrowthSegment(
        id=segment_id(date, start), chat_id=1162433032, chat_name="电丸朱氏会社",
        date=date, start_msg_id=start, end_msg_id=end,
        start_hm="22:22", end_hm="23:07", msg_count=end - start + 1,
        theme="回本心态与价值创造",
        points=["价值是创造出来的，不是节省出来的"],
        quotes=[{"msg_id": start + 5, "sender": "A K", "text": "价值是创造出来的 不是节省出来的"}],
        participants="A K, J1mmy Ding", score=score, status=status, **kw)


def test_insert_and_exact_duplicate(tmp_path: Path):
    db = tmp_path / "t.db"
    first = insert_segments(db, [_seg("2026-07-10", 100, 200)])
    assert [s.id for s in first] == ["2026-07-10-100"]
    again = insert_segments(db, [_seg("2026-07-10", 100, 200)])
    assert again == []


def test_overlap_dedup_thresholds(tmp_path: Path):
    db = tmp_path / "t.db"
    insert_segments(db, [_seg("2026-07-10", 100, 200)])  # width 101
    # overlap 150..200 = 51 ids; narrower width 101 → 50.5% ≥ 50% → dropped
    assert insert_segments(db, [_seg("2026-07-10", 150, 250)]) == []
    # overlap 190..200 = 11 ids vs narrower 101 → ~11% < 50% → kept
    kept = insert_segments(db, [_seg("2026-07-10", 190, 290)])
    assert [s.start_msg_id for s in kept] == [190]
    # adjacent, zero overlap → kept
    kept2 = insert_segments(db, [_seg("2026-07-10", 291, 400)])
    assert [s.start_msg_id for s in kept2] == [291]
    # rejected spans still block near-duplicates
    insert_segments(db, [_seg("2026-07-09", 1000, 1100, score=2.0, status="rejected")])
    assert insert_segments(db, [_seg("2026-07-09", 1010, 1090)]) == []


def test_pick_next_prefers_fresh_date_then_backlog_score(tmp_path: Path):
    db = tmp_path / "t.db"
    insert_segments(db, [
        _seg("2026-07-01", 100, 200, score=9.5),          # backlog, higher score
        _seg("2026-07-10", 1000, 1100, score=6.5),        # fresh day, lower score
        _seg("2026-07-05", 500, 600, score=1.0, status="rejected"),
    ])
    fresh = pick_next(db, prefer_date="2026-07-10")
    assert fresh is not None and fresh.id == "2026-07-10-1000"
    mark_sent(db, fresh.id, style="A")
    backlog = pick_next(db, prefer_date="2026-07-10")
    assert backlog is not None and backlog.id == "2026-07-01-100"
    mark_sent(db, backlog.id, style="B")
    assert pick_next(db, prefer_date="2026-07-10") is None  # rejected never picked


def test_mark_sent_and_daily_quota_guard(tmp_path: Path):
    db = tmp_path / "t.db"
    insert_segments(db, [_seg("2026-07-10", 100, 200)])
    assert sent_count_on(db, "2026-07-11") == 0
    mark_sent(db, "2026-07-10-100", style="A", sent_at="2026-07-11T09:31:00+08:00")
    assert sent_count_on(db, "2026-07-11") == 1
    assert sent_count_on(db, "2026-07-12") == 0
    got = pick_next(db, prefer_date="2026-07-10")
    assert got is None  # sent segments leave the queue


def test_mined_days_guard_and_summary(tmp_path: Path):
    db = tmp_path / "t.db"
    assert not day_already_mined(db, 1162433032, "2026-07-10")
    mark_day_mined(db, 1162433032, "2026-07-10", segments_found=2)
    mark_day_mined(db, 1162433032, "2026-07-09", segments_found=0)
    assert day_already_mined(db, 1162433032, "2026-07-10")
    # re-mark (retry after partial failure) is an upsert, not an error
    mark_day_mined(db, 1162433032, "2026-07-10", segments_found=3)
    summary = mined_days_summary(db, 1162433032)
    assert summary == {"days": 2, "first": "2026-07-09", "last": "2026-07-10"}


def test_ab_log_stats_and_latest_pair(tmp_path: Path):
    db = tmp_path / "t.db"
    old = (datetime.now(LOCAL_TZ) - timedelta(days=30)).isoformat(timespec="seconds")
    log_ab(db, "s1", "v1", "A", 8.0, 6.0, "结构清晰", "<b>卡A1</b>", "<b>卡B1</b>", judged_at=old)
    log_ab(db, "s2", "v1", "B", 5.0, 7.5, "叙事更贴", "<b>卡A2</b>", "<b>卡B2</b>")
    log_ab(db, "s3", "v2", "A", 9.0, 8.0, "要点密", "<b>卡A3</b>", "<b>卡B3</b>")
    stats = ab_stats(db, recent_days=7)
    assert stats["total"] == {"A": 2, "B": 1}
    assert stats["recent"] == {"A": 1, "B": 1}  # the 30-day-old A win ages out
    pair = latest_ab_pair(db)
    assert pair["segment_id"] == "s3" and pair["winner"] == "A"
    assert pair["card_b"] == "<b>卡B3</b>"
    # A send-retry re-judges the same segment (append-only log): only the LATEST
    # verdict per segment counts, so the duplicate must not inflate win totals.
    log_ab(db, "s3", "v2", "B", 4.0, 9.0, "重评审翻盘", "<b>卡A3</b>", "<b>卡B3'</b>")
    stats2 = ab_stats(db, recent_days=7)
    assert stats2["total"] == {"A": 1, "B": 2}   # s3 now counts as a single B win
    assert stats2["recent"] == {"A": 0, "B": 2}


def test_queue_stats_and_recent_sent_window(tmp_path: Path):
    db = tmp_path / "t.db"
    insert_segments(db, [
        _seg("2026-07-10", 100, 200),
        _seg("2026-07-09", 300, 400),
        _seg("2026-07-08", 500, 600, score=2.0, status="rejected"),
    ])
    fresh_ts = datetime.now(LOCAL_TZ).isoformat(timespec="seconds")
    stale_ts = (datetime.now(LOCAL_TZ) - timedelta(days=40)).isoformat(timespec="seconds")
    mark_sent(db, "2026-07-10-100", style="A", sent_at=fresh_ts)
    mark_sent(db, "2026-07-09-300", style="B", sent_at=stale_ts)
    assert queue_stats(db) == {"pending": 0, "sent": 2, "rejected": 1}
    recent = recent_sent(db, days=28)
    assert [s.id for s in recent] == ["2026-07-10-100"]
    assert recent[0].points == ["价值是创造出来的，不是节省出来的"]
    assert recent[0].quotes[0]["sender"] == "A K"


def test_write_slice_file_archives_everything(tmp_path: Path):
    seg = _seg("2026-07-10", 1782515, 1782652)
    rows = [
        {"msg_id": 1782515, "sender_name": "A K",
         "content": "其实不是钱的事儿啊", "timestamp": "2026-07-10T14:22:13+00:00"},
        {"msg_id": 1782516, "sender_name": "J1mmy Ding",
         "content": "😂", "timestamp": "2026-07-10T14:22:32+00:00"},  # short msg kept
        {"msg_id": 1782520, "sender_name": "A K",
         "content": "价值是创造出来的 不是节省出来的", "timestamp": "2026-07-10T14:23:38+00:00"},
    ]
    out = tmp_path / "2026" / "07" / "10-1782515.md"
    write_slice_file(seg, rows, out)
    text = out.read_text(encoding="utf-8")
    assert "# 回本心态与价值创造 — 电丸朱氏会社" in text
    assert "- 日期：2026-07-10（北京 22:22–23:07）" in text
    assert "- span：msg 1782515 – 1782652（138 条）" in text
    assert "[1782515] 22:22 A K: 其实不是钱的事儿啊" in text
    assert "[1782516] 22:22 J1mmy Ding: 😂" in text          # archive keeps short msgs
    assert "[1782520] 22:23 A K: 价值是创造出来的 不是节省出来的" in text


def test_ensure_rubric_creates_default_and_parses_version(tmp_path: Path):
    from chat_daily_tg.growth_store import ensure_rubric, rubric_version_of
    rubric = tmp_path / "rubric.md"
    text, version = ensure_rubric(rubric)
    assert rubric.exists() and version == "v1"
    assert "金句必须是对话原话" in text
    rubric.write_text("# 成长卡片评审偏好 v7（2026-08-01）\n- 新规则\n", encoding="utf-8")
    text2, version2 = ensure_rubric(rubric)  # existing file untouched
    assert version2 == "v7" and "新规则" in text2
    assert rubric_version_of("no header at all") == "v0"


def test_regenerate_slice_index(tmp_path: Path):
    from chat_daily_tg.growth_store import regenerate_slice_index
    db = tmp_path / "t.db"
    seg_dir = tmp_path / "segments"
    with_slice = _seg("2026-07-10", 100, 200,
                      slice_path=str(seg_dir / "2026" / "07" / "10-100.md"))
    no_slice = _seg("2026-07-09", 300, 400, score=2.0, status="rejected")  # 无切片不入索引
    insert_segments(db, [with_slice, no_slice])
    mark_sent(db, "2026-07-10-100", style="A")
    out = regenerate_slice_index(db, seg_dir)
    text = out.read_text(encoding="utf-8")
    assert out == seg_dir / "INDEX.md"
    assert "- 2026-07-10 22:22–23:07 · 回本心态与价值创造 · msg 100–200 · [10-100](2026/07/10-100.md)" in text
    assert "300–400" not in text
    # 重复生成不追加（全量重建）
    regenerate_slice_index(db, seg_dir)
    assert out.read_text(encoding="utf-8").count("msg 100–200") == 1
