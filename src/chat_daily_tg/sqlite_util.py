from __future__ import annotations

from dataclasses import dataclass
import sqlite3
from pathlib import Path

# Single shared DB holds the three opportunity stores (permanent / hot_leads /
# repeat_topics) plus the growth-mining stores (growth_segments / growth_ab_log /
# growth_mined_days). WAL + a write transaction per operation replaces the old
# truncate-then-rewrite JSONL stores, which lost the whole file on any
# mid-write interruption (kill -9 / sleep / disk full).

# ``user_version`` is SQLite's built-in migration marker.  Existing databases
# created before this module adopted it start at 0 and safely replay the
# idempotent base schema once; ordinary connections then only apply per-
# connection pragmas instead of reparsing every CREATE TABLE statement.
_SCHEMA_VERSION = 3

_BASE_SCHEMA = """
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

CREATE TABLE IF NOT EXISTS growth_segments (
    id            TEXT PRIMARY KEY,
    chat_id       INTEGER NOT NULL,
    chat_name     TEXT NOT NULL DEFAULT '',
    date          TEXT NOT NULL,
    start_msg_id  INTEGER NOT NULL,
    end_msg_id    INTEGER NOT NULL,
    start_hm      TEXT NOT NULL DEFAULT '',
    end_hm        TEXT NOT NULL DEFAULT '',
    msg_count     INTEGER NOT NULL DEFAULT 0,
    theme         TEXT NOT NULL,
    points_json   TEXT NOT NULL DEFAULT '[]',
    quotes_json   TEXT NOT NULL DEFAULT '[]',
    participants  TEXT NOT NULL DEFAULT '',
    score         REAL NOT NULL DEFAULT 0,
    status        TEXT NOT NULL DEFAULT 'pending',
    mined_at      TEXT NOT NULL DEFAULT '',
    sent_at       TEXT,
    sent_style    TEXT,
    slice_path    TEXT,
    UNIQUE(chat_id, start_msg_id, end_msg_id)
);

CREATE TABLE IF NOT EXISTS growth_ab_log (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    segment_id     TEXT NOT NULL,
    judged_at      TEXT NOT NULL,
    rubric_version TEXT NOT NULL DEFAULT '',
    winner         TEXT NOT NULL,
    score_a        REAL,
    score_b        REAL,
    verdict        TEXT NOT NULL DEFAULT '',
    card_a         TEXT NOT NULL,
    card_b         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS growth_mined_days (
    chat_id         INTEGER NOT NULL,
    date            TEXT NOT NULL,
    mined_at        TEXT NOT NULL,
    segments_found  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (chat_id, date)
);
"""

_INDEX_SCHEMA = """
CREATE INDEX IF NOT EXISTS idx_permanent_status_captured
    ON permanent(status, captured_at DESC);
CREATE INDEX IF NOT EXISTS idx_hot_leads_status_captured
    ON hot_leads(status, captured_at DESC);
CREATE INDEX IF NOT EXISTS idx_repeat_topics_status_last_seen
    ON repeat_topics(status, last_seen DESC);
CREATE INDEX IF NOT EXISTS idx_growth_pending_date_score
    ON growth_segments(date, score DESC, start_msg_id)
    WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_growth_overlap
    ON growth_segments(chat_id, start_msg_id, end_msg_id);
CREATE INDEX IF NOT EXISTS idx_growth_sent_at
    ON growth_segments(status, sent_at);
"""

_GROWTH_CLAIM_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_growth_claimable
    ON growth_segments(status, claim_until, date, score DESC, start_msg_id)
"""


def _open(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@dataclass(frozen=True)
class Database:
    """Shared SQLite database lifecycle.

    ``initialize`` belongs at application bootstrap or deployment time.  The
    compatibility ``connect`` helper still calls it when an old/uninitialized
    database is encountered, so one-off tools and tests remain safe.
    """

    path: Path

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = _open(self.path)
        try:
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if version < 1:
                conn.executescript(_BASE_SCHEMA)
                conn.execute("PRAGMA user_version = 1")
                version = 1
            if version < 2:
                conn.executescript(_INDEX_SCHEMA)
                conn.execute("PRAGMA user_version = 2")
                version = 2
            if version < 3:
                # This migration deliberately does not use one ``executescript``
                # containing two ALTER TABLE statements. If a process is killed
                # between them, SQLite commits the first DDL statement; replaying
                # a blind script then fails on the already-added column forever.
                # A write lock plus a schema check makes v3 safely resumable and
                # serializes two processes starting against the same legacy DB.
                conn.execute("BEGIN IMMEDIATE")
                try:
                    columns = {
                        row["name"]
                        for row in conn.execute("PRAGMA table_info(growth_segments)")
                    }
                    if "claim_run_id" not in columns:
                        conn.execute(
                            "ALTER TABLE growth_segments ADD COLUMN claim_run_id TEXT"
                        )
                    if "claim_until" not in columns:
                        conn.execute(
                            "ALTER TABLE growth_segments ADD COLUMN claim_until TEXT"
                        )
                    conn.execute(_GROWTH_CLAIM_INDEX_SQL)
                    conn.execute("PRAGMA user_version = 3")
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
                version = 3
            conn.commit()
        finally:
            conn.close()

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = _open(self.path)
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if version < _SCHEMA_VERSION:
            conn.close()
            self.initialize()
            conn = _open(self.path)
        return conn


def connect(db_path: Path) -> sqlite3.Connection:
    """Open the shared DB with crash-resistant pragmas and current migrations."""
    return Database(Path(db_path)).connect()
