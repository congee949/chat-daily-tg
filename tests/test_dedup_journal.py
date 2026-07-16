"""Regression pins for dedup_journal.today_counts (2026-07-16 review fixes).

Entries are written directly as JSON lines with explicit ts values so the
counting semantics — effective-skip-only, Beijing-day bucketing — are pinned
independently of record()'s own timestamping.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from chat_daily_tg.dedup_journal import today_counts

_SH = ZoneInfo("Asia/Shanghai")


def _write(path, entries):
    path.write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in entries) + "\n",
        encoding="utf-8",
    )


def test_today_counts_only_effective_skips_count(tmp_path):
    """Report-mode L2 entries (returned='deliver') and annotations are journaled
    but DELIVERED — they must not count as 去重. Only effective skips do:
    L2 stamps `returned`; L1 has no mode, its `action` is the outcome."""
    now = datetime.now(timezone.utc).isoformat()
    p = tmp_path / "journal.jsonl"
    _write(p, [
        # L2 report mode: would-be skip, actually delivered → not counted.
        {"ts": now, "layer": "L2", "action": "skip", "mode": "report",
         "returned": "deliver"},
        # L2 enforce: effective skip → counted.
        {"ts": now, "layer": "L2", "action": "skip", "mode": "enforce",
         "returned": "skip"},
        # L2 annotation: delivered with a footer → not counted.
        {"ts": now, "layer": "L2", "action": "annotate", "mode": "annotate",
         "returned": "annotate"},
        # L1 has no `returned`; action IS the outcome → counted.
        {"ts": now, "layer": "L1", "action": "skip", "reason": "text"},
    ])
    assert today_counts(p) == {"L2": 1, "L1": 1}


def test_today_counts_buckets_by_shanghai_day_not_utc(tmp_path):
    """Timestamps are stored UTC but the footer covers a Beijing day. An entry
    from yesterday 23:00 Asia/Shanghai must not count toward today; an entry at
    06:00 today Asia/Shanghai (= 22:00 UTC yesterday) must — naive UTC-date
    bucketing would get exactly these two wrong."""
    now_local = datetime.now(_SH)
    yesterday_23_local = (now_local - timedelta(days=1)).replace(
        hour=23, minute=0, second=0, microsecond=0)
    today_06_local = now_local.replace(hour=6, minute=0, second=0, microsecond=0)
    p = tmp_path / "journal.jsonl"
    _write(p, [
        {"ts": yesterday_23_local.astimezone(timezone.utc).isoformat(),
         "layer": "L1", "action": "skip", "reason": "text"},
        # Today in Shanghai, yesterday in UTC — the discriminating case.
        {"ts": today_06_local.astimezone(timezone.utc).isoformat(),
         "layer": "L1", "action": "skip", "reason": "url"},
    ])
    assert today_counts(p, tz="Asia/Shanghai") == {"L1": 1}


def test_today_counts_missing_file_is_empty(tmp_path):
    assert today_counts(tmp_path / "nope.jsonl") == {}
