#!/usr/bin/env python3
"""Measure the REAL cross-producer duplication rate before building anything.

Question: how often does a raw-channel post that ChatDaily actually delivered
carry a tweet/article link that x_monitor had ALREADY pushed to the same forum
group? This is the entire value ceiling of the proposed L1x layer (scp-pulled
pushed_index consulted at send time), so the mechanism only ships if this
number clears the gate.

    GO/NO-GO (written before measuring, per the 2026-07-16 design review):
    time-ordered suppressible hits ≤ 1/month  →  DO NOT BUILD the L1x layer;
    close the backlog item with this report instead.

Buckets (one-way v1 = ChatDaily defers to x_monitor, so order matters):
  clean-suppressible  index ts < channel post ts        (v1 would catch)
  same-window         index ts within post ts + 2h      (racy; depends on the
                                                         exact channels run)
  xmon-later          index ts > post ts + 2h           (v1 can never catch)

Read-only by construction: opens messages.db and the seen file read-only,
never writes SeenStore/content_seen, never sends anything.

Usage:
  scp bwg:/root/x_monitor/twitter_seen/.pushed_index.json /tmp/pushed_index.json
  python3 scripts/measure_cross_producer_dup.py --index /tmp/pushed_index.json
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from chat_daily_tg.content_seen import (  # noqa: E402  (shared mapping — runtime uses the same functions)
    canonical_urls,
    is_bare_link_post,
    tweet_keys_from_urls,
)
from chat_daily_tg.telegram_exporter import canonical_chat_ids, parse_timestamp  # noqa: E402

DEFAULT_DB = Path.home() / "Library/Application Support/tg-cli/messages.db"
DEFAULT_SEEN = Path.home() / "chat-daily/raw_channel_seen.txt"
DEFAULT_CONFIG = Path.home() / "chat-daily/config.yaml"
SAME_WINDOW = timedelta(hours=2)  # channels label cadence


def load_index(path: Path) -> dict[str, dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    entries = data.get("entries")
    if not isinstance(entries, dict):
        sys.exit(f"index at {path} has no entries dict — wrong file?")
    return entries


def index_health(entries: dict[str, dict]) -> tuple[datetime, datetime]:
    """Assert the index is alive before trusting 'no overlap' as a result."""
    if not entries:
        sys.exit("HEALTH FAIL: index is EMPTY — fix bwg cross_account_dedup config "
                 "first; 'no data' is not 'no overlap'.")
    ts = sorted(parse_ts(v["ts"]) for v in entries.values())
    age_h = (datetime.now(timezone.utc) - ts[-1]).total_seconds() / 3600
    if age_h > 48:
        sys.exit(f"HEALTH FAIL: newest index entry is {age_h:.0f}h old (>48h) — "
                 "index looks dead; fix bwg first.")
    per_day = Counter(v["ts"][:10] for v in entries.values())
    rate = sum(per_day.values()) / max(len(per_day), 1)
    print(f"index health OK: {len(entries)} entries, {ts[0]:%m-%d}→{ts[-1]:%m-%d}, "
          f"~{rate:.0f}/day, newest {age_h:.1f}h ago")
    return ts[0], ts[-1]


def parse_ts(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def load_channels(config_path: Path) -> list[dict]:
    import yaml
    cfg = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    return cfg.get("sources", {}).get("telegram", {}).get("raw_channels", [])


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--index", required=True, type=Path)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--seen", type=Path, default=DEFAULT_SEEN)
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    args = ap.parse_args()

    entries = load_index(args.index)
    idx_start, idx_end = index_health(entries)

    seen_keys = set(args.seen.read_text(encoding="utf-8").split())
    channels = load_channels(args.config)
    db = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row

    grand = Counter()
    unmatched_x_urls: list[str] = []
    hits: list[dict] = []

    for ch in channels:
        cfg_id = str(ch["id"])
        ids = canonical_chat_ids(cfg_id)
        marks = ",".join("?" for _ in ids)
        rows = db.execute(
            f"SELECT msg_id, content, timestamp FROM messages "
            f"WHERE chat_id IN ({marks}) ORDER BY msg_id",
            sorted(ids),
        ).fetchall()

        c = Counter()
        # Coverage check: private channels are delivered via Telethon and never
        # land in tg-cli's messages.db, so this measurement cannot see them.
        # (科技圈在花: seen up to 42605, messages.db stops at 42312/07-02.)
        max_seen = max(
            (int(k.split(":")[1]) for k in seen_keys if k.startswith(f"{cfg_id}:")),
            default=0,
        )
        max_db = max((r["msg_id"] for r in rows), default=0)
        if max_seen > max_db:
            print(f"  ⚠ {ch.get('name', cfg_id)}: delivered up to msg {max_seen} but "
                  f"messages.db stops at {max_db} — this channel is (partly) "
                  f"INVISIBLE to this measurement (private/Telethon path)")
        for r in rows:
            if f"{cfg_id}:{r['msg_id']}" not in seen_keys:
                continue  # never delivered by ChatDaily → not our surface
            try:
                post_ts = parse_timestamp(r["timestamp"])
            except Exception:
                continue
            if post_ts.astimezone(timezone.utc) < idx_start - SAME_WINDOW:
                continue  # before the index's observable window
            c["pushed_in_window"] += 1
            text = r["content"] or ""
            urls = canonical_urls(text)
            keys = tweet_keys_from_urls(urls)
            for u in urls:
                if ("x.com" in u or "twitter" in u) and not keys:
                    unmatched_x_urls.append(u)
            if not keys:
                continue
            c["tweet_link_posts"] += 1
            bare = is_bare_link_post(text)
            c["bare_link_posts"] += bare
            for k in sorted(keys):
                e = entries.get(k)
                if not e:
                    continue
                ets = parse_ts(e["ts"])
                pts = post_ts.astimezone(timezone.utc)
                if ets < pts:
                    bucket = "clean-suppressible"
                elif ets <= pts + SAME_WINDOW:
                    bucket = "same-window"
                else:
                    bucket = "xmon-later"
                c[f"hit_{bucket}"] += 1
                if bare:
                    c[f"bare_hit_{bucket}"] += 1
                hits.append({
                    "channel": ch.get("name", cfg_id), "msg_id": r["msg_id"],
                    "key": k, "by": e.get("by"), "bucket": bucket, "bare": bare,
                    "post_ts": pts.isoformat(), "index_ts": e["ts"],
                    "text_head": text[:80].replace("\n", " "),
                })
        grand.update(c)
        print(f"\n{ch.get('name', cfg_id)}: pushed={c['pushed_in_window']} "
              f"tweet-link={c['tweet_link_posts']} bare={c['bare_link_posts']} "
              f"hits: clean={c['hit_clean-suppressible']} "
              f"same-window={c['hit_same-window']} later={c['hit_xmon-later']}")

    days = max((idx_end - idx_start).days, 1)
    would = grand["bare_hit_clean-suppressible"]
    print("\n" + "=" * 64)
    print(f"window: {days} days ({idx_start:%m-%d} → {idx_end:%m-%d})")
    print(f"delivered posts in window:        {grand['pushed_in_window']}")
    print(f"posts with tweet/article links:   {grand['tweet_link_posts']}")
    print(f"  …of which bare links:           {grand['bare_link_posts']}")
    print(f"index hits  clean-suppressible:   {grand['hit_clean-suppressible']} "
          f"(bare: {grand['bare_hit_clean-suppressible']})")
    print(f"            same-window:          {grand['hit_same-window']} "
          f"(bare: {grand['bare_hit_same-window']})")
    print(f"            xmon-later (reverse): {grand['hit_xmon-later']} "
          f"(bare: {grand['bare_hit_xmon-later']})")
    print(f"\nWOULD-SUPPRESS (bare ∩ clean): {would}  →  {would / days * 30:.1f}/month")
    verdict = "BUILD" if would / days * 30 > 1 else "NO-GO (≤1/month gate)"
    print(f"GO/NO-GO verdict: {verdict}")
    if unmatched_x_urls:
        print(f"\nunmatched x/twitter URLs (key-space gap, {len(unmatched_x_urls)}):")
        for u in unmatched_x_urls[:10]:
            print("  ", u)
    if hits:
        print("\nhit detail:")
        for h in hits:
            print(f"  [{h['bucket']}{' BARE' if h['bare'] else ''}] {h['channel']} "
                  f"msg {h['msg_id']} {h['key']} by @{h['by']}\n"
                  f"      post {h['post_ts']} vs index {h['index_ts']}\n"
                  f"      {h['text_head']}")


if __name__ == "__main__":
    main()
