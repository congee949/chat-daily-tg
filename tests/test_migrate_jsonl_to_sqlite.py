"""Regression tests for scripts/migrate_jsonl_to_sqlite.py.

Two silent-data-loss paths the migration used to have, neither previously
covered by a test:

1. Rows were inserted from the raw legacy ``rec`` dict instead of the validated
   dataclass. A legacy record missing a NOT-NULL-defaulted field (``status`` /
   ``mention_count`` / ...) produced a NULL column → IntegrityError → swallowed
   by ``INSERT OR IGNORE`` → the row was silently dropped while the script still
   reported it among the rows read.

2. Two historical ``permanent`` records whose raw URLs differ only by
   utm/share tracking tokens now collapse to the same canonical fingerprint.
   ``INSERT OR IGNORE`` dropped the second silently — no ``mention_count`` merge,
   no per-row warning.

These tests build legacy JSONL fixtures that trigger each path and assert the
migration imports/merges them correctly and reports the collapse instead of
dropping silently.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from chat_daily_tg.sqlite_util import connect


def _load_migrate():
    """Load scripts/migrate_jsonl_to_sqlite.py as a module (it is not a package)."""
    path = Path(__file__).resolve().parent.parent / "scripts" / "migrate_jsonl_to_sqlite.py"
    spec = importlib.util.spec_from_file_location("migrate_jsonl_to_sqlite", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


migrate = _load_migrate()


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _run_migration(tmp_path, monkeypatch, *, perm=None, hot=None, rt=None, argv=("--force",)):
    """Wire the migration's module-level paths to a temp data dir and run main()."""
    data = tmp_path / "data"
    db_path = data / "chat-daily.db"
    perm_jsonl = data / "permanent.jsonl"
    rt_jsonl = data / "repeat_topics.jsonl"
    hot_dir = data / "hot-leads"

    if perm is not None:
        _write_jsonl(perm_jsonl, perm)
    if rt is not None:
        _write_jsonl(rt_jsonl, rt)
    if hot is not None:
        _write_jsonl(hot_dir / "2026" / "01" / "01.jsonl", hot)

    monkeypatch.setattr(migrate, "DB_PATH", db_path)
    monkeypatch.setattr(migrate, "PERMANENT_JSONL", perm_jsonl)
    monkeypatch.setattr(migrate, "REPEAT_TOPICS_JSONL", rt_jsonl)
    monkeypatch.setattr(migrate, "HOT_LEADS_DIR", hot_dir)
    monkeypatch.setattr(sys, "argv", ["migrate", *argv])

    rc = migrate.main()
    return rc, db_path


def _perm_full(**over) -> dict:
    rec = {
        "id": "p-full",
        "captured_at": "2026-01-01",
        "source_group": "G",
        "source_sender": "A",
        "category": "activity",
        "type": "activity",
        "title": "返现活动",
        "content": "内容",
        "url": "https://e.com/a?id=5",
        "mention_count": 1,
        "status": "alive",
    }
    rec.update(over)
    return rec


def _rt_full(**over) -> dict:
    rec = {
        "id": "t-full",
        "title": "话题",
        "first_seen": "2026-01-01",
        "last_seen": "2026-01-02",
        "seen_dates": ["2026-01-01", "2026-01-02"],
        "mention_count": 2,
        "last_summary": "摘要",
        "status": "active",
        "last_source_group": "G",
        "last_source_sender": "A",
    }
    rec.update(over)
    return rec


def _hot_full(**over) -> dict:
    rec = {
        "id": "h-full",
        "captured_at": "2026-01-01",
        "title": "热点",
        "summary": "摘要",
        "category": "arbitrage",
        "source_group": "G",
        "source_sender": "A",
        "status": "alive",
    }
    rec.update(over)
    return rec


# --- Path 1: legacy record missing a NOT-NULL-defaulted field -------------


def test_permanent_record_missing_defaulted_fields_is_imported(tmp_path, monkeypatch):
    """A permanent row missing status + mention_count must land with defaults,
    not get NULL'd into a dropped IntegrityError."""
    rec = _perm_full()
    rec.pop("status")
    rec.pop("mention_count")

    rc, db_path = _run_migration(tmp_path, monkeypatch, perm=[rec])
    assert rc == 0

    conn = connect(db_path)
    try:
        rows = conn.execute("SELECT id, status, mention_count FROM permanent").fetchall()
    finally:
        conn.close()

    assert len(rows) == 1, "row missing a defaulted field was silently dropped"
    assert rows[0]["status"] == "alive"        # dataclass default applied
    assert rows[0]["mention_count"] == 1       # dataclass default applied


def test_repeat_topic_missing_defaulted_fields_is_imported(tmp_path, monkeypatch):
    """A repeat_topics row missing status/last_source_group/last_source_sender
    must land with defaults rather than NULL → dropped."""
    rec = _rt_full()
    for k in ("status", "last_source_group", "last_source_sender"):
        rec.pop(k)

    rc, db_path = _run_migration(tmp_path, monkeypatch, rt=[rec])
    assert rc == 0

    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT status, last_source_group, last_source_sender FROM repeat_topics"
        ).fetchall()
    finally:
        conn.close()

    assert len(rows) == 1, "repeat_topic missing a defaulted field was silently dropped"
    assert rows[0]["status"] == "active"
    assert rows[0]["last_source_group"] == ""
    assert rows[0]["last_source_sender"] == ""


def test_hot_lead_missing_optional_fields_is_imported(tmp_path, monkeypatch):
    """hot_leads built from the dataclass: optional fields absent → imported."""
    rec = _hot_full()
    rec.pop("risk_notes", None)
    rec.pop("death_signal", None)

    rc, db_path = _run_migration(tmp_path, monkeypatch, hot=[rec])
    assert rc == 0

    conn = connect(db_path)
    try:
        rows = conn.execute("SELECT id, risk_notes FROM hot_leads").fetchall()
    finally:
        conn.close()

    assert len(rows) == 1
    assert rows[0]["risk_notes"] is None


def test_reported_db_count_reflects_actually_imported_rows(tmp_path, monkeypatch, capsys):
    """The 'DB now holds' line is a real COUNT(*), so a dropped row can't be
    reported as imported."""
    good = _perm_full(id="ok")
    missing = _perm_full(id="dropme", url="https://e.com/other")
    missing.pop("status")
    missing.pop("mention_count")

    rc, db_path = _run_migration(tmp_path, monkeypatch, perm=[good, missing])
    assert rc == 0

    conn = connect(db_path)
    try:
        n = conn.execute("SELECT COUNT(*) FROM permanent").fetchone()[0]
    finally:
        conn.close()

    assert n == 2, "the record missing a defaulted field never made it into the DB"
    out = capsys.readouterr().out
    assert "DB now holds" in out
    assert "'permanent': 2" in out


# --- Path 2: canonical-fingerprint collapse -------------------------------


def test_fingerprint_collapse_merges_mention_count(tmp_path, monkeypatch):
    """Two records, same canonical URL but different utm/share tokens, must
    collapse to one row with the historical mention_counts merged — not the
    second silently dropped."""
    a = _perm_full(id="a", url="https://e.com/a?id=9&utm_source=wx", mention_count=3)
    b = _perm_full(id="b", url="https://e.com/a?id=9&share=1&utm_source=weibo",
                   mention_count=2, title="同活动不同标题")

    rc, db_path = _run_migration(tmp_path, monkeypatch, perm=[a, b])
    assert rc == 0

    conn = connect(db_path)
    try:
        rows = conn.execute("SELECT mention_count FROM permanent").fetchall()
    finally:
        conn.close()

    assert len(rows) == 1, "fingerprint-colliding records did not collapse to one row"
    assert rows[0]["mention_count"] == 5, "merged mention_count was lost (3 + 2 expected)"


def test_fingerprint_collapse_is_reported_not_silent(tmp_path, monkeypatch, capsys):
    """The collapse must be counted/reported, so the drop is never silent."""
    a = _perm_full(id="a", url="https://e.com/a?id=9&utm_source=wx", mention_count=3)
    b = _perm_full(id="b", url="https://e.com/a?id=9&share=1", mention_count=2)

    rc, _ = _run_migration(tmp_path, monkeypatch, perm=[a, b])
    assert rc == 0

    out = capsys.readouterr().out
    assert "canonical-fingerprint" in out
    assert "merged 1" in out


def test_distinct_fingerprints_are_not_merged(tmp_path, monkeypatch):
    """Guard: identity-bearing query params still distinguish opportunities."""
    a = _perm_full(id="a", url="https://e.com/a?id=9", mention_count=1)
    b = _perm_full(id="b", url="https://e.com/a?id=10", mention_count=1)

    rc, db_path = _run_migration(tmp_path, monkeypatch, perm=[a, b])
    assert rc == 0

    conn = connect(db_path)
    try:
        n = conn.execute("SELECT COUNT(*) FROM permanent").fetchone()[0]
    finally:
        conn.close()

    assert n == 2
