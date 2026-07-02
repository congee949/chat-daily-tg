#!/usr/bin/env python
"""One-off migration: legacy JSONL stores → chat-daily.db (SQLite).

Imports permanent.jsonl, repeat_topics.jsonl and hot-leads/**/*.jsonl into the
single shared SQLite DB. Tolerant of corrupt/half-written lines (the exact
failure the old truncate-rewrite stores left behind). Backs up every legacy
file with a timestamp before touching anything.

Usage:
    .venv/bin/python scripts/migrate_jsonl_to_sqlite.py [--force] [--dry-run]

Refuses to run if the DB already holds rows, unless --force.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

# Allow running as a plain script (src/ layout).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from chat_daily_tg.paths import (  # noqa: E402
    DB_PATH, PERMANENT_JSONL, REPEAT_TOPICS_JSONL, HOT_LEADS_DIR,
)
from chat_daily_tg.sqlite_util import connect  # noqa: E402
from chat_daily_tg.db import PermanentEntry, compute_fingerprint  # noqa: E402
from chat_daily_tg.hot_leads import HotLead  # noqa: E402
from chat_daily_tg.repeat_topics import RepeatTopic  # noqa: E402

PERM_COLS = (
    "id", "fingerprint", "captured_at", "source_group", "source_sender",
    "category", "type", "title", "content", "url", "expires_at",
    "last_mentioned_at", "mention_count", "status", "death_signal", "notes",
)
HOT_COLS = (
    "id", "captured_at", "title", "summary", "category", "source_group",
    "source_sender", "status", "risk_notes", "death_signal",
)
RT_COLS = (
    "id", "title", "first_seen", "last_seen", "seen_dates", "mention_count",
    "last_summary", "status", "last_source_group", "last_source_sender",
    "last_new_information",
)


def _read_jsonl(path: Path) -> tuple[list[dict], int]:
    """Return (records, bad_line_count); skips blank and corrupt lines."""
    records, bad = [], 0
    if not path.exists():
        return records, bad
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                bad += 1
    return records, bad


def _backup(path: Path, ts: str) -> None:
    if path.exists():
        dst = path.with_name(path.name + f".bak-{ts}")
        shutil.copy2(path, dst)
        print(f"  backup: {path.name} → {dst.name}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true",
                    help="import even if the DB already has rows")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would be imported, write nothing")
    args = ap.parse_args()

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    db_existed = DB_PATH.exists()
    conn = connect(DB_PATH)
    try:
        existing = {
            t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("permanent", "hot_leads", "repeat_topics")
        }
        if any(existing.values()) and not args.force:
            print(f"DB already populated {existing}; refusing without --force.")
            return 1

        # --- gather (tolerant of corrupt lines) ---
        perm_recs, perm_bad = _read_jsonl(PERMANENT_JSONL)
        rt_recs, rt_bad = _read_jsonl(REPEAT_TOPICS_JSONL)
        hot_recs, hot_bad = [], 0
        if HOT_LEADS_DIR.exists():
            for jl in sorted(HOT_LEADS_DIR.rglob("*.jsonl")):
                recs, bad = _read_jsonl(jl)
                hot_recs.extend(recs)
                hot_bad += bad

        print(f"permanent: {len(perm_recs)} rows ({perm_bad} corrupt skipped)")
        print(f"repeat_topics: {len(rt_recs)} rows ({rt_bad} corrupt skipped)")
        print(f"hot_leads: {len(hot_recs)} rows ({hot_bad} corrupt skipped)")
        if args.dry_run:
            print("dry-run: nothing written.")
            conn.close()
            # connect() created the DB to read counts; honor "writes nothing".
            if not db_existed:
                for suffix in ("", "-wal", "-shm"):
                    Path(str(DB_PATH) + suffix).unlink(missing_ok=True)
            return 0

        # --- backup legacy files ---
        print("backing up legacy files:")
        _backup(PERMANENT_JSONL, ts)
        _backup(REPEAT_TOPICS_JSONL, ts)
        if HOT_LEADS_DIR.exists():
            for jl in HOT_LEADS_DIR.rglob("*.jsonl"):
                _backup(jl, ts)

        # --- import. Build each row from the VALIDATED dataclass (asdict), not the
        # raw record, so a legacy row missing a defaulted field gets its default
        # rather than NULL → a NOT-NULL violation silently dropped by OR IGNORE. ---
        #
        # permanent: two historical rows can carry different raw utm/share URLs
        # that now reduce to the same canonical fingerprint (review finding #3).
        # OR IGNORE alone would drop the second silently and strand its
        # mention_count. Pre-merge by fingerprint instead: sum mention_count and
        # count the collapses so the merge is reported, never silent.
        perm_by_fp: dict[str, dict] = {}
        perm_collapsed = 0
        for rec in perm_recs:
            e = PermanentEntry(**rec)
            row = asdict(e)
            row["fingerprint"] = e.fingerprint()
            prev = perm_by_fp.get(row["fingerprint"])
            if prev is None:
                perm_by_fp[row["fingerprint"]] = row
            else:
                prev["mention_count"] = (prev["mention_count"] or 0) + (row["mention_count"] or 0)
                perm_collapsed += 1

        # hot_leads ids are POSITIONAL ({date}-hot-NNN); the old blind-append wrote
        # multiple same-day catch-up reruns into one day file, so one id routinely
        # maps to DIFFERENT leads. INSERT OR IGNORE-by-id would silently drop every
        # distinct collision (observed: 95 of 234 rows, all distinct content). Keep
        # every distinct lead by re-id'ing collisions; collapse only byte-identical
        # rows.
        hot_rows: list[dict] = []
        hot_seen_sig: set = set()
        hot_used_ids: set = set()
        hot_exact_dup = hot_reid = 0
        for rec in hot_recs:
            row = asdict(HotLead(**rec))
            sig = tuple(sorted((k, repr(v)) for k, v in row.items()))
            if sig in hot_seen_sig:
                hot_exact_dup += 1
                continue
            hot_seen_sig.add(sig)
            if row["id"] in hot_used_ids:
                base, n = row["id"], 1
                while f"{base}-r{n}" in hot_used_ids:
                    n += 1
                row["id"] = f"{base}-r{n}"
                hot_reid += 1
            hot_used_ids.add(row["id"])
            hot_rows.append(row)

        skipped = {"permanent": 0, "repeat_topics": 0}
        with conn:
            for row in perm_by_fp.values():
                cur = conn.execute(
                    f"INSERT OR IGNORE INTO permanent ({', '.join(PERM_COLS)}) "
                    f"VALUES ({', '.join('?' for _ in PERM_COLS)})",
                    [row[c] for c in PERM_COLS],
                )
                skipped["permanent"] += (cur.rowcount == 0)
            for row in hot_rows:
                conn.execute(
                    f"INSERT INTO hot_leads ({', '.join(HOT_COLS)}) "
                    f"VALUES ({', '.join('?' for _ in HOT_COLS)})",
                    [row[c] for c in HOT_COLS],
                )
            for rec in rt_recs:
                row = asdict(RepeatTopic(**rec))
                row["seen_dates"] = json.dumps(row.get("seen_dates", []), ensure_ascii=False)
                cur = conn.execute(
                    f"INSERT OR IGNORE INTO repeat_topics ({', '.join(RT_COLS)}) "
                    f"VALUES ({', '.join('?' for _ in RT_COLS)})",
                    [row[c] for c in RT_COLS],
                )
                skipped["repeat_topics"] += (cur.rowcount == 0)
        if perm_collapsed:
            print(f"permanent: merged {perm_collapsed} canonical-fingerprint "
                  f"duplicate(s) (mention_count summed)")
        if hot_reid or hot_exact_dup:
            print(f"hot_leads: re-id'd {hot_reid} positional-id collision(s), "
                  f"dropped {hot_exact_dup} byte-identical duplicate(s)")
        if any(skipped.values()):
            print(f"skipped (duplicate id): {skipped}")

        final = {
            t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("permanent", "hot_leads", "repeat_topics")
        }
        print(f"done. DB now holds {final}")
        print(f"DB: {DB_PATH}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
