#!/usr/bin/env python3
"""Compare the old scan-shaped high-water lookup with SeenStore's index.

This is a local microbenchmark, not a CI timing gate. It also prints a
work-count comparison so the conclusion does not depend only on wall-clock
noise.
"""
from __future__ import annotations

import argparse
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter

from chat_daily_tg.raw_seen import SeenStore


def scan_max(seen: set[str], chat_id: str) -> int:
    prefix = f"{chat_id}:"
    best = 0
    for key in seen:
        if key.startswith(prefix):
            try:
                best = max(best, int(key[len(prefix):]))
            except ValueError:
                continue
    return best


def timed(fn, repeats: int) -> tuple[float, int]:
    started = perf_counter()
    result = 0
    for _ in range(repeats):
        result = fn()
    return perf_counter() - started, result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--channels", type=int, default=50)
    parser.add_argument("--messages-per-channel", type=int, default=2_000)
    parser.add_argument("--queries", type=int, default=2_000)
    args = parser.parse_args()

    with TemporaryDirectory() as temp_dir:
        path = Path(temp_dir) / "seen.txt"
        with path.open("w", encoding="utf-8") as handle:
            for channel in range(args.channels):
                for msg_id in range(1, args.messages_per_channel + 1):
                    handle.write(f"-100{channel}:{msg_id}\n")
            handle.write("youtube:non-numeric-id\n")

        store = SeenStore(path)
        target = f"-100{args.channels // 2}"
        expected = args.messages_per_channel

        scan_seconds, scan_result = timed(
            lambda: scan_max(store._seen, target), args.queries
        )
        indexed_seconds, indexed_result = timed(
            lambda: store.max_msg_id(target), args.queries
        )

        assert scan_result == indexed_result == expected
        scanned_keys = len(store._seen) * args.queries
        indexed_lookups = args.queries
        speedup = scan_seconds / max(indexed_seconds, 1e-12)
        print(f"keys={len(store._seen):,} queries={args.queries:,}")
        print(f"scan baseline: {scan_seconds:.6f}s; key visits={scanned_keys:,}")
        print(f"indexed:       {indexed_seconds:.6f}s; dict lookups={indexed_lookups:,}")
        print(f"wall-clock speedup: {speedup:.1f}x")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
