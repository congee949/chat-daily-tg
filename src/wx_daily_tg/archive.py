from __future__ import annotations
from pathlib import Path
from wx_daily_tg.paths import ARCHIVE_DIR


def safe_filename(name: str) -> str:
    """Sanitize group name for use as filename. Keep unicode; strip only unsafe chars."""
    unsafe = "/:\\\x00"
    out = name
    for c in unsafe:
        out = out.replace(c, "_")
    return out


def prepare_archive_day(date_str: str) -> Path:
    """Create archive/YYYY/MM/DD/ and return path."""
    y, m, d = date_str.split("-")
    p = ARCHIVE_DIR / y / m / d
    p.mkdir(parents=True, exist_ok=True)
    return p
