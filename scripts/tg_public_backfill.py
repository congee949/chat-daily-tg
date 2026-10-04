"""Oldest-first incremental Telegram sync for public channel forwarding.

Run under the kabi-tg-cli interpreter so it reuses the existing logged-in
Telethon session:

    <python> tg_public_backfill.py <chat_id> <db_path> <limit> <min_id>

The complete page is fetched before one atomic SQLite UPSERT. A Telethon failure
therefore leaves the DB cursor unchanged, and the caller's write-after-send HWM
remains the sole delivery cursor.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys

import tg_cli.client as tc


tc._default_api_warned = True


_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    platform      TEXT    NOT NULL DEFAULT 'telegram',
    chat_id       INTEGER NOT NULL,
    chat_name     TEXT,
    msg_id        INTEGER NOT NULL,
    sender_id     INTEGER,
    sender_name   TEXT,
    content       TEXT,
    timestamp     TEXT    NOT NULL,
    raw_json      TEXT,
    UNIQUE(platform, chat_id, msg_id)
);
CREATE INDEX IF NOT EXISTS idx_messages_chat_ts ON messages(chat_id, timestamp);
"""


def _sender_name(sender) -> str | None:
    if sender is None:
        return None
    title = getattr(sender, "title", None)
    if title:
        return str(title)
    parts = [
        getattr(sender, "first_name", None),
        getattr(sender, "last_name", None),
    ]
    name = " ".join(str(part) for part in parts if part).strip()
    if name:
        return name
    username = getattr(sender, "username", None)
    return f"@{username}" if username else None


def message_row(message, *, chat_id: int, chat_name: str | None) -> dict:
    timestamp = message.date or datetime.now(timezone.utc)
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    sender = getattr(message, "_sender", None) or getattr(message, "sender", None)
    raw = {}
    grouped_id = getattr(message, "grouped_id", None)
    if grouped_id is not None:
        raw["grouped_id"] = grouped_id
    if getattr(message, "fwd_from", None) is not None:
        raw["fwd_from"] = True
    return {
        "platform": "telegram",
        "chat_id": chat_id,
        "chat_name": chat_name,
        "msg_id": int(message.id),
        "sender_id": getattr(message, "sender_id", None),
        "sender_name": _sender_name(sender),
        "content": message.text or message.message or "",
        "timestamp": timestamp.isoformat(),
        "raw_json": json.dumps(raw, ensure_ascii=False) if raw else None,
    }


async def fetch_oldest_page(client, entity, *, limit: int,
                            min_id: int) -> list:
    """Fetch the oldest bounded page above the exclusive durable cursor."""
    messages = []
    async for message in client.iter_messages(
            entity, limit=limit, reverse=True, offset_id=min_id):
        messages.append(message)
    return messages


def upsert_rows(db_path: str | Path, rows: list[dict]) -> int:
    """Atomically persist a complete fetched page; return changed row count."""
    if not rows:
        return 0
    path = Path(db_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30)
    try:
        conn.executescript(_SCHEMA)
        before = conn.total_changes
        with conn:
            conn.executemany(
                """INSERT INTO messages (
                       platform, chat_id, chat_name, msg_id, sender_id,
                       sender_name, content, timestamp, raw_json
                   ) VALUES (
                       :platform, :chat_id, :chat_name, :msg_id, :sender_id,
                       :sender_name, :content, :timestamp, :raw_json
                   )
                   ON CONFLICT(platform, chat_id, msg_id) DO UPDATE SET
                       chat_name=excluded.chat_name,
                       sender_id=excluded.sender_id,
                       sender_name=excluded.sender_name,
                       content=excluded.content,
                       timestamp=excluded.timestamp,
                       raw_json=excluded.raw_json""",
                rows,
            )
        return conn.total_changes - before
    finally:
        conn.close()


async def backfill_page(client, entity, *, db_path: str | Path, limit: int,
                        min_id: int) -> dict:
    messages = await fetch_oldest_page(
        client, entity, limit=limit, min_id=min_id)
    chat_id = int(entity.id)
    chat_name = (
        getattr(entity, "title", None)
        or getattr(entity, "first_name", None)
        or str(chat_id)
    )
    rows = [message_row(m, chat_id=chat_id, chat_name=chat_name)
            for m in messages]
    changed = upsert_rows(db_path, rows)
    return {
        "fetched": len(rows),
        "upserted": changed,
        "first_id": rows[0]["msg_id"] if rows else None,
        "last_id": rows[-1]["msg_id"] if rows else None,
    }


async def main() -> int:
    chat_id = int(sys.argv[1])
    db_path = sys.argv[2]
    limit = int(sys.argv[3])
    min_id = int(sys.argv[4])
    if limit <= 0 or min_id <= 0:
        raise ValueError("limit and min_id must be positive")
    async with tc.connect() as client:
        entity = await client.get_entity(chat_id)
        stats = await backfill_page(
            client, entity, db_path=db_path, limit=limit, min_id=min_id)
    print(json.dumps(stats, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
