from __future__ import annotations
from pathlib import Path
import shutil

DATA_DIR = Path.home() / "chat-daily"
LEGACY_DATA_DIR = Path.home() / "wx-daily"
CONFIG_PATH = DATA_DIR / "config.yaml"
PERMANENT_JSONL = DATA_DIR / "permanent.jsonl"
PERMANENT_MD = DATA_DIR / "permanent.md"
REPEAT_TOPICS_JSONL = DATA_DIR / "repeat_topics.jsonl"
HOT_LEADS_DIR = DATA_DIR / "hot-leads"
HOT_LEADS_LATEST = HOT_LEADS_DIR / "latest.md"
ARCHIVE_DIR = DATA_DIR / "archive"
LOG_DIR = DATA_DIR / "logs"


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
