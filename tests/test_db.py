from pathlib import Path
from chat_daily_tg.db import PermanentDB, PermanentEntry, compute_fingerprint
from chat_daily_tg.sqlite_util import Database


def _make(title: str, url=None, category="bank_product", content="c", captured_at="2026-04-17T10:00:00") -> PermanentEntry:
    return PermanentEntry(
        id=f"test-{title[:4]}", captured_at=captured_at,
        source_group="G", source_sender="A",
        category=category, type="permanent", title=title, content=content, url=url,
    )


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


def test_fingerprint_url_based_is_stable_across_title_drift():
    fp1 = compute_fingerprint("中行卓隽Plus放水", url="https://x.com/a", category="bank_product")
    fp2 = compute_fingerprint("中国银行卓隽Plus信用卡（放水）", url="https://x.com/a", category="bank_product")
    assert fp1 == fp2


def test_fingerprint_title_based_when_url_missing():
    fp1 = compute_fingerprint("恒生银行CNID线上开户", url=None, category="bank_product")
    fp2 = compute_fingerprint("恒生银行CNID线上开户", url="", category="bank_product")
    fp3 = compute_fingerprint("恒生银行支持CNID线上开户", url=None, category="bank_product")
    assert fp1 == fp2
    assert fp1 != fp3  # strict title-based fp — drift handled by migration script


def test_upsert_inserts_new_entry(tmp_path: Path):
    db = PermanentDB(path=tmp_path / "permanent.jsonl")
    action, saved = db.upsert(_make("恒生银行CNID线上开户"))
    assert action == "inserted"
    assert saved.mention_count == 1
    assert len(list(db.read_all())) == 1


def test_upsert_merges_on_same_fingerprint(tmp_path: Path):
    db = PermanentDB(path=tmp_path / "permanent.jsonl")
    db.upsert(_make("恒生银行CNID线上开户", content="v1", captured_at="2026-04-18T10:00:00"))
    action, saved = db.upsert(_make("恒生银行CNID线上开户", content="v2", captured_at="2026-04-19T10:00:00"))
    assert action == "updated"
    assert saved.mention_count == 2
    assert saved.content == "v2"
    assert saved.last_mentioned_at == "2026-04-19T10:00:00"
    assert len(list(db.read_all())) == 1


def test_upsert_merges_url_match_even_if_titles_drift(tmp_path: Path):
    db = PermanentDB(path=tmp_path / "permanent.jsonl")
    db.upsert(_make("万事达美团返现50元活动", url="https://priceless.com/x", category="activity"))
    action, saved = db.upsert(_make("万事达卡美团返现50", url="https://priceless.com/x", category="activity"))
    assert action == "updated"
    assert saved.mention_count == 2
    assert len(list(db.read_all())) == 1


def test_upsert_many_mixed_insert_update(tmp_path: Path):
    db = PermanentDB(path=tmp_path / "permanent.db")
    db.upsert(_make("恒生银行CNID线上开户", content="v1"))

    results = db.upsert_many([
        _make("恒生银行CNID线上开户", content="v2", captured_at="2026-04-19T10:00:00"),
        _make("万事达返现", url="https://x.com/a", category="activity"),
        _make("中行卓隽Plus", content="p1"),
    ])
    assert [a for a, _ in results] == ["updated", "inserted", "inserted"]
    rows = list(db.read_all())
    assert len(rows) == 3


def test_fingerprint_strips_tracking_params():
    # Same activity link shared with different utm/share tokens → one identity.
    bare = compute_fingerprint("活动", url="https://e.com/a?id=5", category="activity")
    utm = compute_fingerprint("活动", url="https://e.com/a?id=5&utm_source=tg&from=group",
                              category="activity")
    assert bare == utm
    # An identity-bearing param still distinguishes different opportunities.
    other = compute_fingerprint("活动", url="https://e.com/a?id=6", category="activity")
    assert bare != other


def test_upsert_collapses_utm_duplicates(tmp_path: Path):
    db = PermanentDB(path=tmp_path / "permanent.db")
    db.upsert(_make("活动甲", url="https://e.com/a?id=5", category="activity"))
    action, saved = db.upsert(
        _make("活动甲", url="https://e.com/a?id=5&utm_source=tg", category="activity"))
    assert action == "updated"
    assert saved.mention_count == 2
    assert len(list(db.read_all())) == 1


def test_database_initializes_once_and_records_schema_version(tmp_path: Path):
    database = Database(tmp_path / "shared.db")
    database.initialize()
    conn = database.connect()
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        indexes = {row["name"] for row in conn.execute("PRAGMA index_list('growth_segments')")}
    finally:
        conn.close()
    assert version == 3
    assert "idx_growth_claimable" in indexes


def test_database_resumes_partially_applied_growth_claim_migration(tmp_path: Path):
    """An interrupted v3 migration may have committed its first ALTER only."""
    import sqlite3
    from chat_daily_tg import sqlite_util

    path = tmp_path / "partial-v3.db"
    conn = sqlite3.connect(path)
    try:
        conn.executescript(sqlite_util._BASE_SCHEMA)
        conn.executescript(sqlite_util._INDEX_SCHEMA)
        conn.execute("ALTER TABLE growth_segments ADD COLUMN claim_run_id TEXT")
        conn.execute("PRAGMA user_version = 2")
        conn.commit()
    finally:
        conn.close()

    Database(path).initialize()
    conn = Database(path).connect()
    try:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(growth_segments)")}
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()
    assert {"claim_run_id", "claim_until"}.issubset(columns)
    assert version == 3
