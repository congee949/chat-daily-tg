from __future__ import annotations
from pathlib import Path
from chat_daily_tg.paths import archive_dir_for


def safe_filename(name: str) -> str:
    """Sanitize group name for use as filename. Replaces /, :, \\, NUL with _; keeps unicode/emoji."""
    unsafe = "/:\\\x00"
    out = name
    for c in unsafe:
        out = out.replace(c, "_")
    return out


def prepare_archive_day(date_str: str) -> Path:
    """Create archive/YYYY/MM/DD/ and return path."""
    p = archive_dir_for(date_str)
    p.mkdir(parents=True, exist_ok=True)
    return p
