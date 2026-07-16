from __future__ import annotations
from pathlib import Path
import shutil

DATA_DIR = Path.home() / "chat-daily"
LEGACY_DATA_DIR = Path.home() / "wx-daily"
CONFIG_PATH = DATA_DIR / "config.yaml"

# Single shared SQLite store for permanent / hot_leads / repeat_topics.
DB_PATH = DATA_DIR / "chat-daily.db"

# Legacy JSONL stores — no longer written. Kept only so the one-off migration
# (scripts/migrate_jsonl_to_sqlite.py) can find and import the old data.
PERMANENT_JSONL = DATA_DIR / "permanent.jsonl"
REPEAT_TOPICS_JSONL = DATA_DIR / "repeat_topics.jsonl"

# Human-readable derived views (regenerated from the DB each run).
PERMANENT_MD = DATA_DIR / "permanent.md"
HOT_LEADS_DIR = DATA_DIR / "hot-leads"
HOT_LEADS_LATEST = HOT_LEADS_DIR / "latest.md"
ARCHIVE_DIR = DATA_DIR / "archive"
LOG_DIR = DATA_DIR / "logs"

# Bilibili digest dedup (raw_seen.SeenStore, keys "bilibili:<bvid>").
BILIBILI_SEEN_PATH = DATA_DIR / "bilibili_seen.txt"

# Dedup layer state (content_seen / topic_dedup / cross-producer copies).
STATE_DIR = DATA_DIR / "state"
CONTENT_SEEN_DB = STATE_DIR / "content_seen.db"
DELIVERED_INDEX_DB = STATE_DIR / "delivered_index.db"
XMONITOR_INDEX_COPY = STATE_DIR / "xmonitor_pushed_index.json"
DEDUP_JOURNAL = STATE_DIR / "dedup_journal.jsonl"

# Growth mining (个人成长 topic) artifacts.
GROWTH_DIR = DATA_DIR / "growth"
GROWTH_SEGMENTS_DIR = GROWTH_DIR / "segments"
GROWTH_RUBRIC = GROWTH_DIR / "rubric.md"
GROWTH_RUBRIC_HISTORY = GROWTH_DIR / "rubric-history"
GROWTH_OFFSET_PATH = GROWTH_DIR / "getupdates-offset.txt"
GROWTH_FEEDBACK_INBOX = GROWTH_DIR / "feedback-inbox.jsonl"


def migrate_legacy_config_if_needed(path: Path = CONFIG_PATH) -> None:
    """Copy the old wx-daily config on first chat-daily run, without deleting old data."""
    target = Path(path).expanduser()
    legacy = LEGACY_DATA_DIR / "config.yaml"
    if target.exists() or target != CONFIG_PATH or not legacy.exists():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(legacy, target)


def archive_dir_for(date_str: str) -> Path:
    """`date_str` is YYYY-MM-DD → returns archive/YYYY/MM/DD path."""
    y, m, d = date_str.split("-")
    return ARCHIVE_DIR / y / m / d


def hot_leads_day_file(date_str: str) -> Path:
    """`date_str` is YYYY-MM-DD → returns hot-leads/YYYY/MM/DD.md path."""
    y, m, d = date_str.split("-")
    return HOT_LEADS_DIR / y / m / f"{d}.md"


def log_file_for(date_str: str) -> Path:
    return LOG_DIR / f"{date_str}.log"


def growth_slice_file(date_str: str, start_msg_id: int) -> Path:
    """`date_str` is YYYY-MM-DD → returns growth/segments/YYYY/MM/DD-<start_id>.md."""
    y, m, d = date_str.split("-")
    return GROWTH_SEGMENTS_DIR / y / m / f"{d}-{start_msg_id}.md"
