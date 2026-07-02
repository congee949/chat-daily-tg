from __future__ import annotations
from datetime import date, timedelta
import shutil
from pathlib import Path
from chat_daily_tg import paths
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


_MEDIA_DIR_NAMES = ("tg_media", "wx_media")


def cleanup_old_media(retention_days: int) -> tuple[int, int]:
    """Delete tg_media/wx_media dirs in day-dirs older than retention_days.

    Downloaded media is re-fetchable next run; the text archive (summary.md,
    vision.jsonl, etc.) in the same day-dir is left untouched — it's small and
    is the actual permanent record. Returns (dirs removed, bytes freed).
    """
    cutoff = date.today() - timedelta(days=retention_days)
    removed_dirs = 0
    freed_bytes = 0
    if not paths.ARCHIVE_DIR.exists():
        return 0, 0
    for day_dir in paths.ARCHIVE_DIR.glob("*/*/*"):
        if not day_dir.is_dir():
            continue
        try:
            day = date(int(day_dir.parent.parent.name), int(day_dir.parent.name), int(day_dir.name))
        except ValueError:
            continue
        if day >= cutoff:
            continue
        for name in _MEDIA_DIR_NAMES:
            media_dir = day_dir / name
            if not media_dir.is_dir():
                continue
            freed_bytes += sum(f.stat().st_size for f in media_dir.rglob("*") if f.is_file())
            shutil.rmtree(media_dir, ignore_errors=True)
            removed_dirs += 1
    return removed_dirs, freed_bytes
