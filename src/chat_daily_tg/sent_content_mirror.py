"""Validated, expiring snapshots of confirmed BWG content deliveries."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA = "chatdaily.sent-content-mirror.v1"
MAX_BYTES = 64 * 1024 * 1024


def utc(value: str) -> datetime:
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("timestamp requires timezone")
    return stamp.astimezone(timezone.utc)


def validate_rows(rows: list) -> list[dict]:
    if not isinstance(rows, list) or not rows:
        raise ValueError("empty or invalid ledger")
    valid = {}
    for row in rows:
        if not isinstance(row, dict) or row.get("schema") != "sent-content.v1":
            raise ValueError("invalid ledger schema")
        if row.get("delivery_state") != "confirmed":
            raise ValueError("unconfirmed delivery")
        for field in ("chat_id", "message_id"):
            if type(row.get(field)) is not int or row[field] == 0:
                raise ValueError("invalid delivery id")
        if row["message_id"] < 1 or (row.get("thread_id") is not None and
                (type(row["thread_id"]) is not int or row["thread_id"] < 1)):
            raise ValueError("invalid message/thread id")
        if row.get("producer") not in ("x_monitor", "macrumors"):
            raise ValueError("unexpected producer")
        content = row.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("empty caption")
        if hashlib.sha256(content.encode()).hexdigest() != row.get("content_hash"):
            raise ValueError("caption hash mismatch")
        stamp = utc(row["sent_at"])
        if stamp > datetime.now(timezone.utc) + timedelta(minutes=5):
            raise ValueError("future delivery")
        item = dict(row, sent_at=stamp.isoformat())
        key = (row["chat_id"], row["message_id"])
        if key in valid and valid[key] != item:
            raise ValueError("conflicting delivery mapping")
        valid[key] = item
    return list(valid.values())


def digest_rows(rows: list[dict]) -> str:
    return hashlib.sha256(json.dumps(rows, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":")).encode()).hexdigest()


def write_snapshot(raw: bytes, destination: Path, *, source: str) -> dict:
    if len(raw) > MAX_BYTES:
        raise ValueError("ledger exceeds size limit")
    rows = validate_rows([json.loads(line) for line in raw.splitlines() if line.strip()])
    # An append-only source must retain every prior delivery, even after a long outage.
    if destination.exists():
        previous = json.loads(destination.read_bytes())
        if previous.get("schema") != SCHEMA:
            raise ValueError("unexpected existing snapshot")
        old_rows = validate_rows(previous["rows"])
        current = {(r["chat_id"], r["message_id"]): r for r in rows}
        if any(current.get((r["chat_id"], r["message_id"])) != r for r in old_rows):
            raise ValueError("ledger shrank or rewrote a confirmed delivery")
    payload = {"schema": SCHEMA, "source": source,
               "fetched_at": datetime.now(timezone.utc).isoformat(),
               "rows_sha256": digest_rows(rows), "rows": rows}
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=destination.name + ".", dir=destination.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(payload, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, destination)
    finally:
        Path(name).unlink(missing_ok=True)
    return {"rows": len(rows), "fetched_at": payload["fetched_at"], "status": "synced"}


def read_snapshot(path: Path, *, max_age_hours: float = 24) -> dict:
    if not 0 < max_age_hours <= 24 or path.stat().st_size > MAX_BYTES:
        raise ValueError("invalid snapshot size/age limit")
    snapshot = json.loads(path.read_bytes())
    if snapshot.get("schema") != SCHEMA:
        raise ValueError("invalid snapshot schema")
    age = (datetime.now(timezone.utc) - utc(snapshot["fetched_at"])).total_seconds()
    if age < -300 or age > max_age_hours * 3600:
        raise ValueError("stale snapshot")
    rows = validate_rows(snapshot["rows"])
    if digest_rows(rows) != snapshot.get("rows_sha256"):
        raise ValueError("snapshot digest mismatch")
    return dict(snapshot, rows=rows)
