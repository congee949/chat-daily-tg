"""Append-only journal of every dedup suppression / annotation decision.

A wrong suppression is permanent and invisible — the SeenStore write is
terminal and every input to the decision (index copies, rolling windows)
self-destructs. This journal is the ONLY durable record of why a post was
withheld, and it doubles as the raw data for measuring precision. Shared by
the L1 (content_seen) and L2 (topic_dedup) layers so neither imports the other.

Writing never raises: journaling failure must not block delivery, and a
suppression that cannot be journaled still proceeds (it is already logged).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from chat_daily_tg.paths import DEDUP_JOURNAL

log = logging.getLogger(__name__)


def record(entry: dict, path: Path = DEDUP_JOURNAL) -> None:
    """Append one JSON line: {ts, layer, action, chat_id, msg_id, ...}."""
    try:
        entry = {"ts": datetime.now(timezone.utc).isoformat(), **entry}
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        log.warning("dedup journal write failed (suppression already logged): %s", e)


def today_counts(path: Path = DEDUP_JOURNAL, tz: str = "Asia/Shanghai") -> dict[str, int]:
    """{'L1': n, 'L2': m} posts actually WITHHELD today (local `tz` day) — feeds
    the daily-report footer. Report-mode and annotate entries are journaled but
    delivered, so they must not count as 去重: only entries whose effective
    outcome was a skip qualify (L2 stamps `returned`; L1 has no mode, its
    `action` is the outcome). Timestamps are stored UTC; the report covers a
    Beijing day, so bucketing converts. Any read problem returns {} (footer
    omitted)."""
    counts: dict[str, int] = {}
    try:
        from zoneinfo import ZoneInfo
        local = ZoneInfo(tz)
        today = datetime.now(local).date()
        with Path(path).open(encoding="utf-8") as f:
            for line in f:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                outcome = str(e.get("returned", e.get("action", "")))
                if outcome != "skip":
                    continue
                try:
                    ts = datetime.fromisoformat(str(e.get("ts", "")).replace("Z", "+00:00"))
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                except ValueError:
                    continue
                if ts.astimezone(local).date() == today:
                    layer = str(e.get("layer", "?"))
                    counts[layer] = counts.get(layer, 0) + 1
    except FileNotFoundError:
        pass
    except Exception as e:
        log.warning("dedup journal read failed: %s", e)
        return {}
    return counts
