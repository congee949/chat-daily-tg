from datetime import date, timedelta
from pathlib import Path
from chat_daily_tg.hot_leads import (
    HotLead, append_day_leads, regenerate_latest, load_all_leads, mark_lead_status,
)


def _lead(**kw) -> HotLead:
    base = dict(
        id="hl-1", captured_at="2026-04-17", title="Example tool price",
        summary="example source", category="arbitrage",
        source_group="G1", source_sender="Alice", status="alive",
    )
    base.update(kw)
    return HotLead(**base)


def test_append_writes_md_when_md_root_given(tmp_path: Path):
    db = tmp_path / "hl.db"
    p = append_day_leads(db, "2026-04-17", [_lead()], md_root=tmp_path)
    assert p is not None
    assert p.exists()
    assert "Example tool price" in p.read_text(encoding="utf-8")
    # And the data landed in the DB.
    assert [l.id for l in load_all_leads(db)] == ["hl-1"]


def test_append_empty_does_not_create_file(tmp_path: Path):
    db = tmp_path / "hl.db"
    p = append_day_leads(db, "2026-04-17", [], md_root=tmp_path)
    assert p is None
    assert not (tmp_path / "2026" / "04" / "17.md").exists()


def test_append_same_id_rerun_is_idempotent(tmp_path: Path):
    # Catch-up rerun must not create duplicate rows (finding #40).
    db = tmp_path / "hl.db"
    append_day_leads(db, "2026-04-17", [_lead(summary="v1")])
    append_day_leads(db, "2026-04-17", [_lead(summary="v2")])
    leads = load_all_leads(db)
    assert len(leads) == 1
    assert leads[0].summary == "v2"


def test_regenerate_latest_excludes_expired(tmp_path: Path):
    db = tmp_path / "hl.db"
    today = date.today()
    append_day_leads(db, today.isoformat(), [_lead(id="fresh", title="fresh",
                     captured_at=today.isoformat())])
    append_day_leads(db, (today - timedelta(days=20)).isoformat(),
                     [_lead(id="expired", title="expired",
                            captured_at=(today - timedelta(days=20)).isoformat())])

    latest = tmp_path / "latest.md"
    regenerate_latest(db, latest, retention_days=14)
    text = latest.read_text(encoding="utf-8")
    assert "fresh" in text
    assert "expired" not in text


def test_regenerate_latest_excludes_dead(tmp_path: Path):
    db = tmp_path / "hl.db"
    today = date.today()
    append_day_leads(db, today.isoformat(), [
        _lead(id="a", title="alive", captured_at=today.isoformat(), status="alive"),
        _lead(id="d", title="dead", captured_at=today.isoformat(), status="dead"),
    ])
    regenerate_latest(db, tmp_path / "latest.md", retention_days=14)
    text = (tmp_path / "latest.md").read_text(encoding="utf-8")
    assert "alive" in text
    assert "dead" not in text


def test_mark_lead_status_updates_db(tmp_path: Path):
    db = tmp_path / "hl.db"
    append_day_leads(db, "2026-04-17", [_lead(id="lead-x", title="X")])

    assert mark_lead_status(db, "lead-x", status="dead", death_signal="关门了") is True

    leads = load_all_leads(db)
    assert len(leads) == 1
    assert leads[0].status == "dead"
    assert leads[0].death_signal == "关门了"


def test_mark_lead_status_returns_false_if_not_found(tmp_path: Path):
    assert mark_lead_status(tmp_path / "hl.db", "nonexistent", status="dead") is False
