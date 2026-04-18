from pathlib import Path
from wx_daily_tg.db import PermanentDB, PermanentEntry


def test_append_and_read(tmp_path: Path):
    db = PermanentDB(path=tmp_path / "permanent.jsonl")
    e = PermanentEntry(
        id="2026-04-17-foo",
        captured_at="2026-04-17T10:00:00+08:00",
        source_group="G1",
        source_sender="Alice",
        category="invite_code",
        type="permanent",
        title="Foo invite",
        content="ABC123",
    )
    db.append(e)
    entries = list(db.read_all())
    assert len(entries) == 1
    assert entries[0].id == "2026-04-17-foo"


def test_mark_dead(tmp_path: Path):
    db = PermanentDB(path=tmp_path / "permanent.jsonl")
    db.append(PermanentEntry(
        id="e1", captured_at="2026-04-17", source_group="G", source_sender="A",
        category="invite_code", type="permanent", title="t", content="c",
    ))
    db.append(PermanentEntry(
        id="e2", captured_at="2026-04-17", source_group="G", source_sender="A",
        category="invite_code", type="permanent", title="t2", content="c2",
    ))
    db.mark_status("e1", status="dead", death_signal="关门了")
    entries = list(db.read_all())
    e1 = next(e for e in entries if e.id == "e1")
    e2 = next(e for e in entries if e.id == "e2")
    assert e1.status == "dead"
    assert e1.death_signal == "关门了"
    assert e2.status == "alive"


def test_find_by_id_returns_none_if_missing(tmp_path: Path):
    db = PermanentDB(path=tmp_path / "permanent.jsonl")
    assert db.find("nonexistent") is None
