from __future__ import annotations
from datetime import date, timedelta
from pathlib import Path
from chat_daily_tg.db import PermanentDB
from chat_daily_tg.hot_leads import load_all_leads
from chat_daily_tg.repeat_topics import recent_repeat_summary


def active_permanent_summary(db_path: Path, max_items: int = 50) -> str:
    """Short markdown listing alive permanent entries (id + title + category)."""
    db = PermanentDB(db_path)
    lines = []
    for e in db.read_all():
        if e.status != "alive":
            continue
        lines.append(f"- `{e.id}` [{e.category}] {e.title}")
        if len(lines) >= max_items:
            break
    if not lines:
        return "(空)"
    return "\n".join(lines)


def active_hot_leads_summary(root: Path, retention_days: int = 14,
                              max_items: int = 50) -> str:
    cutoff = date.today() - timedelta(days=retention_days)
    leads = load_all_leads(root)
    lines = []
    for l in leads:
        if l.status != "alive":
            continue
        if date.fromisoformat(l.captured_at) < cutoff:
            continue
        lines.append(f"- `{l.id}` [{l.category}] {l.title} ({l.captured_at})")
        if len(lines) >= max_items:
            break
    if not lines:
        return "(空)"
    return "\n".join(lines)


def active_repeat_topics_summary(path: Path, today: str, cooldown_days: int = 7,
                                 max_items: int = 30) -> str:
    return recent_repeat_summary(
        path=path,
        today=today,
        cooldown_days=cooldown_days,
        max_items=max_items,
    )
