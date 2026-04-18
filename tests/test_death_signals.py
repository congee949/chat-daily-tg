from pathlib import Path
from wx_daily_tg.db import PermanentDB, PermanentEntry
from wx_daily_tg.hot_leads import HotLead, append_day_leads, load_all_leads
from wx_daily_tg.death_signals import apply_death_signals


def test_apply_high_confidence_marks_dead(tmp_path: Path):
    db = PermanentDB(tmp_path / "p.jsonl")
    db.append(PermanentEntry(
        id="target1", captured_at="2026-04-17", source_group="G", source_sender="A",
        category="invite_code", type="permanent", title="Target", content="X",
    ))
    signals = [
        {"target_title_or_id": "target1", "signal_text": "关门了",
         "signal_source": "Bob in G2 18:00", "confidence": "high"},
    ]
    applied = apply_death_signals(
        signals, db_path=tmp_path / "p.jsonl", hot_leads_root=tmp_path / "hl",
    )
    assert applied == 1
    e = db.find("target1")
    assert e.status == "dead"
    assert e.death_signal == "关门了"


def test_apply_medium_confidence_marks_likely_dead(tmp_path: Path):
    db = PermanentDB(tmp_path / "p.jsonl")
    db.append(PermanentEntry(
        id="target1", captured_at="2026-04-17", source_group="G", source_sender="A",
        category="invite_code", type="permanent", title="Target", content="X",
    ))
    signals = [
        {"target_title_or_id": "target1", "signal_text": "好像不行了",
         "signal_source": "X", "confidence": "medium"},
    ]
    apply_death_signals(signals, db_path=tmp_path / "p.jsonl",
                        hot_leads_root=tmp_path / "hl")
    assert db.find("target1").status == "likely_dead"


def test_apply_low_confidence_ignored(tmp_path: Path):
    db = PermanentDB(tmp_path / "p.jsonl")
    db.append(PermanentEntry(
        id="target1", captured_at="2026-04-17", source_group="G", source_sender="A",
        category="invite_code", type="permanent", title="Target", content="X",
    ))
    apply_death_signals(
        [{"target_title_or_id": "target1", "signal_text": "?",
          "signal_source": "X", "confidence": "low"}],
        db_path=tmp_path / "p.jsonl", hot_leads_root=tmp_path / "hl",
    )
    assert db.find("target1").status == "alive"


def test_target_matched_by_title_fallback(tmp_path: Path):
    db = PermanentDB(tmp_path / "p.jsonl")
    db.append(PermanentEntry(
        id="long-id-abc", captured_at="2026-04-17", source_group="G",
        source_sender="A", category="invite_code", type="permanent",
        title="Chase vx 2x 打法", content="...",
    ))
    applied = apply_death_signals(
        [{"target_title_or_id": "Chase vx 2x 打法", "signal_text": "关门了",
          "signal_source": "X", "confidence": "high"}],
        db_path=tmp_path / "p.jsonl", hot_leads_root=tmp_path / "hl",
    )
    assert applied == 1
    assert db.find("long-id-abc").status == "dead"
