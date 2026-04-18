from __future__ import annotations
from pathlib import Path

DATA_DIR = Path.home() / "wx-daily"
CONFIG_PATH = DATA_DIR / "config.yaml"
PERMANENT_JSONL = DATA_DIR / "permanent.jsonl"
PERMANENT_MD = DATA_DIR / "permanent.md"
HOT_LEADS_DIR = DATA_DIR / "hot-leads"
HOT_LEADS_LATEST = HOT_LEADS_DIR / "latest.md"
ARCHIVE_DIR = DATA_DIR / "archive"
LOG_DIR = DATA_DIR / "logs"


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
