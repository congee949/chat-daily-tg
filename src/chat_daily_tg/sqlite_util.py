from __future__ import annotations

import sqlite3
from pathlib import Path

# Single shared DB holds the three opportunity stores (permanent / hot_leads /
# repeat_topics). WAL + a write transaction per operation replaces the old
# truncate-then-rewrite JSONL stores, which lost the whole file on any
# mid-write interruption (kill -9 / sleep / disk full).

_SCHEMA = """
CREATE TABLE IF NOT EXISTS permanent (
    id                 TEXT PRIMARY KEY,
    fingerprint        TEXT UNIQUE NOT NULL,
    captured_at        TEXT NOT NULL,
    source_group       TEXT NOT NULL DEFAULT '',
    source_sender      TEXT NOT NULL DEFAULT '',
    category           TEXT NOT NULL,
    type               TEXT NOT NULL,
    title              TEXT NOT NULL,
    content            TEXT NOT NULL DEFAULT '',
    url                TEXT,
    expires_at         TEXT,
    last_mentioned_at  TEXT,
    mention_count      INTEGER NOT NULL DEFAULT 1,
    status             TEXT NOT NULL DEFAULT 'alive',
    death_signal       TEXT,
    notes              TEXT
);

CREATE TABLE IF NOT EXISTS hot_leads (
    id             TEXT PRIMARY KEY,
    captured_at    TEXT NOT NULL,
    title          TEXT NOT NULL DEFAULT '',
    summary        TEXT NOT NULL DEFAULT '',
    category       TEXT NOT NULL DEFAULT '',
    source_group   TEXT NOT NULL DEFAULT '',
    source_sender  TEXT NOT NULL DEFAULT '',
    status         TEXT NOT NULL DEFAULT 'alive',
    risk_notes     TEXT,
    death_signal   TEXT
);

CREATE TABLE IF NOT EXISTS repeat_topics (
    id                    TEXT PRIMARY KEY,
    title                 TEXT NOT NULL,
    first_seen            TEXT NOT NULL,
    last_seen             TEXT NOT NULL,
    seen_dates            TEXT NOT NULL DEFAULT '[]',
    mention_count         INTEGER NOT NULL DEFAULT 1,
    last_summary          TEXT NOT NULL DEFAULT '',
    status                TEXT NOT NULL DEFAULT 'active',
    last_source_group     TEXT NOT NULL DEFAULT '',
    last_source_sender    TEXT NOT NULL DEFAULT '',
    last_new_information  TEXT
);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    """Open the shared DB with crash-resistant pragmas and the schema applied."""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(_SCHEMA)
    return conn
