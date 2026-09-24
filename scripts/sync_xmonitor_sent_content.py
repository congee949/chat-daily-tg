#!/usr/bin/env python3
"""Pull confirmed BWG captions into an atomic Mac snapshot. --check performs no writes."""
from __future__ import annotations
import argparse
import json
import shlex
import subprocess
import sys
from pathlib import Path

# Allow direct invocation from a checkout without requiring an installed package.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from chat_daily_tg.paths import XMONITOR_SENT_COPY
from chat_daily_tg.sent_content_mirror import read_snapshot, write_snapshot

SSH_OPTS = ("-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
            "-o", "StrictHostKeyChecking=accept-new")


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--host", default="bwg")
    p.add_argument("--remote", default="/root/x_monitor/state/x_monitor_sent_content_ledger.jsonl")
    p.add_argument("--destination", type=Path, default=XMONITOR_SENT_COPY)
    p.add_argument("--source-file", type=Path, help="use a downloaded ledger instead of SSH")
    p.add_argument("--check", action="store_true")
    args = p.parse_args(argv)
    try:
        if args.check:
            snap = read_snapshot(args.destination)
            report = {"status": "fresh", "rows": len(snap["rows"]), "fetched_at": snap["fetched_at"]}
        else:
            if args.source_file:
                raw = args.source_file.read_bytes()
            else:
                raw = subprocess.run(
                    ["ssh", *SSH_OPTS, args.host,
                     "cat -- " + shlex.quote(args.remote)], check=True, capture_output=True,
                    timeout=30).stdout
            report = write_snapshot(raw, args.destination, source=args.host + ":" + args.remote)
        print(json.dumps(report, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(json.dumps({"status": "unavailable", "error": type(exc).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
