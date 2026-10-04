#!/usr/bin/env python3
"""Bounded, side-band Qwen backfill for delivered_index.db.

Dry-run is the default and opens SQLite read-only.  ``--apply`` only migrates
the derived delivered schema and updates embedding/provenance columns. An explicit sent-ledger option also imports confirmed captions from a local snapshot. It never
syncs Telegram, sends, advances seen, writes ledgers/markers, or VACUUMs.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

from chat_daily_tg.config import Config
from chat_daily_tg.evidence_index import (
    EmbeddingGeneration,
    EmbeddingValidationError,
    GENERATION_METADATA_COLUMNS,
    build_embedder,
    decode_vector,
    generation_row_matches,
    validate_vector,
)
from chat_daily_tg.topic_dedup import DeliveredIndex


def _read_config(path: Path) -> Config:
    # Do not call load_config(): its legacy migration is a write and this
    # maintenance command promises dry-run is byte-for-byte read-only.
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("config must be a YAML object")
    return Config(**raw)


def _dry_coverage(
    path: Path,
    *,
    generation,
    window_hours: int,
) -> dict[str, Any]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(delivered)")}
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=window_hours)).isoformat()
        rows = conn.execute(
            "SELECT * FROM delivered WHERE ts>=? AND norm_text!=''", (cutoff,)
        ).fetchall()
        valid = missing = incompatible = invalid = 0
        for row in rows:
            embedding = row["embedding"]
            if embedding is None:
                missing += 1
                continue
            metadata_columns = {name for name, _ in GENERATION_METADATA_COLUMNS}
            if not metadata_columns.issubset(columns):
                incompatible += 1
                continue
            if (
                not generation_row_matches(row, generation)
                or not isinstance(embedding, bytes)
                or len(embedding) != generation.dimension * 4
            ):
                incompatible += 1
                continue
            try:
                validate_vector(decode_vector(embedding), generation=generation)
            except EmbeddingValidationError:
                invalid += 1
            else:
                valid += 1
        eligible = len(rows)
        return {
            "eligible_rows": eligible,
            "valid_rows": valid,
            "missing_rows": missing,
            "incompatible_rows": incompatible,
            "invalid_rows": invalid,
            "ratio": valid / eligible if eligible else 1.0,
            "schema_columns": sorted(columns),
        }
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--window-hours", type=int, default=336)
    parser.add_argument("--max-rows", type=int, default=256)
    parser.add_argument("--batch-size", type=int, default=16, choices=range(1, 33))
    parser.add_argument("--max-seconds", type=float, default=300.0)
    parser.add_argument("--sent-ledger", type=Path,
                        help="import a validated caption snapshot before bounded backfill")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    if not args.db.is_file():
        parser.error(f"database does not exist: {args.db}")
    if not args.config.is_file():
        parser.error(f"config does not exist: {args.config}")
    if args.window_hours < 1 or args.max_rows <= 0 or args.max_seconds <= 0:
        parser.error("window-hours/max-rows/max-seconds must be positive")

    cfg = _read_config(args.config)
    em = cfg.models.embedding if cfg.models else None
    if not (em and em.enabled):
        raise ValueError("models.embedding must be enabled")
    generation = EmbeddingGeneration.from_config(em)
    mirror_rows = 0
    if args.sent_ledger is not None:
        from chat_daily_tg.sent_content_mirror import read_snapshot
        snapshot = read_snapshot(args.sent_ledger,
            max_age_hours=cfg.sources.telegram.dedup.topic.xmonitor_ledger_max_age_hours)
        mirror_rows = len(snapshot["rows"])
    before = _dry_coverage(
        args.db, generation=generation, window_hours=args.window_hours
    )
    if not args.apply:
        print(
            json.dumps(
                {
                    "status": "dry-run",
                    "apply": False,
                    "generation_id": generation.generation_id,
                    "model_id": generation.model_id,
                    "dimension": generation.dimension,
                    "before": before,
                    "database_unchanged": True,
                    "mirror_rows_available": mirror_rows,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0 if before["ratio"] >= 0.995 else 3

    if em.provider != "openai":
        raise ValueError(
            "--apply requires provider=openai so the absolute Qwen deadline is enforceable"
        )

    # Client construction (and credential lookup for remote providers) belongs
    # only to apply mode.  Clip each individual request timeout to the CLI's
    # remaining wall-clock budget; row and wall limits are both checked at
    # every transaction boundary below.
    started = time.monotonic()
    deadline = started + args.max_seconds
    em = em.model_copy(
        update={
            "batch_size": args.batch_size,
            "timeout": min(float(em.timeout), args.max_seconds),
        }
    )
    embedder = build_embedder(em)
    index = DeliveredIndex(
        args.db,
        window_days=max(1, (args.window_hours + 23) // 24),
        generation=generation,
        prune_on_open=False,
    )
    updated = 0
    imported = 0
    try:
        if args.sent_ledger is not None:
            topic = cfg.sources.telegram.dedup.topic
            imported = index.ingest_sent_ledger(
                args.sent_ledger, topic.forum_chat_id,
                max_age_hours=topic.xmonitor_ledger_max_age_hours)
        while updated < args.max_rows:
            if time.monotonic() >= deadline:
                break
            cap = min(args.batch_size, args.max_rows - updated)
            count = index.backfill_embeddings(
                embedder,
                cap=cap,
                window_hours=args.window_hours,
                deadline=deadline,
            )
            if count <= 0:
                break
            updated += count
        coverage = index.coverage(window_hours=args.window_hours)
        after = {
            "eligible_rows": coverage.eligible_rows,
            "valid_rows": coverage.valid_rows,
            "missing_rows": coverage.missing_rows,
            "incompatible_rows": coverage.incompatible_rows,
            "invalid_rows": coverage.invalid_rows,
            "ratio": coverage.ratio,
        }
    finally:
        index.close()
    print(
        json.dumps(
            {
                "status": "ready" if after["ratio"] >= 0.995 else "partial",
                "apply": True,
                "generation_id": generation.generation_id,
                "model_id": generation.model_id,
                "dimension": generation.dimension,
                "selected_limit": args.max_rows,
                "updated": updated,
                "mirror_rows_imported": imported,
                "elapsed_seconds": round(time.monotonic() - started, 2),
                "before": before,
                "after": after,
                "remaining": max(0, after["eligible_rows"] - after["valid_rows"]),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if after["ratio"] >= 0.995 else 3


if __name__ == "__main__":
    raise SystemExit(main())
