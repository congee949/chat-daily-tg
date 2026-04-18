from datetime import date, timedelta
from pathlib import Path
from wx_daily_tg.hot_leads import (
    HotLead, append_day_leads, regenerate_latest, load_all_leads,
)


def test_append_only_creates_file_when_nonempty(tmp_path: Path):
    leads = [HotLead(
        id="hl-1", captured_at="2026-04-17", title="OpenAI low plus",
        summary="86GameStore source", category="arbitrage",
        source_group="G1", source_sender="Alice", status="alive",
    )]
    p = append_day_leads(tmp_path, "2026-04-17", leads)
    assert p is not None
    assert p.exists()
    assert "OpenAI low plus" in p.read_text(encoding="utf-8")


def test_append_empty_does_not_create_file(tmp_path: Path):
    p = append_day_leads(tmp_path, "2026-04-17", [])
    assert p is None
    assert not (tmp_path / "2026" / "04" / "17.md").exists()


def test_regenerate_latest_excludes_expired(tmp_path: Path):
    today = date.today()
    fresh = HotLead(
        id="fresh", captured_at=today.isoformat(), title="fresh", summary="",
        category="arbitrage", source_group="G", source_sender="A", status="alive",
    )
    expired = HotLead(
        id="expired",
        captured_at=(today - timedelta(days=20)).isoformat(),
        title="expired", summary="",
        category="arbitrage", source_group="G", source_sender="A", status="alive",
    )
    append_day_leads(tmp_path, fresh.captured_at, [fresh])
    append_day_leads(tmp_path, expired.captured_at, [expired])

    latest = tmp_path / "latest.md"
    regenerate_latest(tmp_path, latest, retention_days=14)
    text = latest.read_text(encoding="utf-8")
    assert "fresh" in text
    assert "expired" not in text


def test_regenerate_latest_excludes_dead(tmp_path: Path):
    today = date.today()
    alive = HotLead(id="a", captured_at=today.isoformat(), title="alive", summary="",
                    category="arbitrage", source_group="G", source_sender="A",
                    status="alive")
    dead = HotLead(id="d", captured_at=today.isoformat(), title="dead", summary="",
                   category="arbitrage", source_group="G", source_sender="A",
                   status="dead")
    append_day_leads(tmp_path, today.isoformat(), [alive, dead])
    regenerate_latest(tmp_path, tmp_path / "latest.md", retention_days=14)
    text = (tmp_path / "latest.md").read_text(encoding="utf-8")
    assert "alive" in text
    assert "dead" not in text


def test_mark_lead_status_updates_jsonl(tmp_path: Path):
    lead = HotLead(
        id="lead-x", captured_at="2026-04-17", title="X", summary="",
        category="arbitrage", source_group="G", source_sender="A", status="alive",
    )
    append_day_leads(tmp_path, "2026-04-17", [lead])

    from wx_daily_tg.hot_leads import mark_lead_status
    updated = mark_lead_status(tmp_path, "lead-x", status="dead", death_signal="关门了")
    assert updated is True

    # Verify the change persisted
    leads = load_all_leads(tmp_path)
    assert len(leads) == 1
    assert leads[0].status == "dead"
    assert leads[0].death_signal == "关门了"


def test_mark_lead_status_returns_false_if_not_found(tmp_path: Path):
    from wx_daily_tg.hot_leads import mark_lead_status
    # No leads exist yet
    assert mark_lead_status(tmp_path, "nonexistent", status="dead") is False
