from datetime import date, timedelta
from pathlib import Path
from wx_daily_tg.db import PermanentDB, PermanentEntry
from wx_daily_tg.hot_leads import HotLead, append_day_leads
from wx_daily_tg.context_builder import (
    active_permanent_summary, active_hot_leads_summary,
)


def test_active_permanent_summary_lists_alive(tmp_path: Path):
    db = PermanentDB(tmp_path / "p.jsonl")
    db.append(PermanentEntry(
        id="alive1", captured_at="2026-04-17", source_group="G", source_sender="A",
        category="invite_code", type="permanent", title="Alive invite", content="X",
        status="alive",
    ))
    db.append(PermanentEntry(
        id="dead1", captured_at="2026-04-17", source_group="G", source_sender="A",
        category="invite_code", type="permanent", title="Dead invite", content="Y",
        status="dead",
    ))
    s = active_permanent_summary(db.path, max_items=50)
    assert "Alive invite" in s
    assert "Dead invite" not in s
    assert "alive1" in s


def test_active_hot_leads_summary_only_within_window(tmp_path: Path):
    today = date.today()
    append_day_leads(tmp_path, today.isoformat(), [
        HotLead(id="fresh", captured_at=today.isoformat(), title="Fresh lead",
                summary="", category="arbitrage", source_group="G",
                source_sender="A", status="alive"),
    ])
    append_day_leads(tmp_path, (today - timedelta(days=30)).isoformat(), [
        HotLead(id="old", captured_at=(today - timedelta(days=30)).isoformat(),
                title="Old lead", summary="", category="arbitrage",
                source_group="G", source_sender="A", status="alive"),
    ])
    s = active_hot_leads_summary(tmp_path, retention_days=14)
    assert "Fresh lead" in s
    assert "Old lead" not in s
