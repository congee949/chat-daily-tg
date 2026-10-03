"""Append-only ledger for content successfully delivered to Telegram.

This is deliberately separate from ``media_sent_ledger.jsonl``.  The latter is
an r4s-owned Podcast handoff whose Mac replica is atomically replaced by sync;
general chat-daily delivery records must survive that replacement.
"""
from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from chat_daily_tg.paths import SENT_CONTENT_LEDGER


_lock = threading.Lock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _coerce_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def content_sha256(content: str) -> str:
    """Stable hash of the exact UTF-8 content stored in the ledger row."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def append_message_ids(
    message_ids: Iterable[int | str] | int | str | None,
    *,
    chat_id: int | str,
    thread_id: int | str | None,
    producer: str,
    source_kind: str,
    source_ref: str,
    source_message_ids: Iterable[int | str],
    url: str,
    content: str,
    content_id: str | None = None,
    path: str | Path | None = None,
    sent_at: str | None = None,
    source_messages: list[dict[str, Any]] | None = None,
) -> int:
    """Append one confirmed-delivery row per target Telegram message id.

    Callers invoke this only after their sender has returned successfully.  I/O
    errors intentionally propagate so the delivery path can log and suppress
    them without confusing a ledger failure with a Telegram send failure.
    """
    target_chat_id = _coerce_int(chat_id)
    target_thread_id = _coerce_int(thread_id)
    if isinstance(message_ids, (int, str)):
        raw_target_ids = [message_ids]
    else:
        raw_target_ids = list(message_ids or [])
    target_ids = [mid for value in raw_target_ids if (mid := _coerce_int(value)) is not None]
    source_ids = [mid for value in source_message_ids if (mid := _coerce_int(value)) is not None]

    if (
        target_chat_id is None
        or not target_ids
        or not producer
        or not source_kind
        or not source_ref
        or not source_ids
        or not url
        or not content
    ):
        return 0

    original = None
    if source_messages is not None:
        if ([item.get("message_id") for item in source_messages] != source_ids
                or any(not isinstance(item.get("text"), str) for item in source_messages)):
            raise ValueError("original messages must match source IDs in order")
        original = [{"message_id": item["message_id"], "text": item["text"]}
                    for item in source_messages]

    timestamp = sent_at or _now_iso()
    digest = content_sha256(content)
    dest = Path(path).expanduser() if path is not None else SENT_CONTENT_LEDGER
    rows = []
    for message_id in target_ids:
        row: dict[str, Any] = {
            "schema": "sent-content.v1",
            "chat_id": target_chat_id,
            "thread_id": target_thread_id,
            "message_id": message_id,
            "producer": producer,
            "source_kind": source_kind,
            "source_ref": source_ref,
            "source_message_ids": source_ids,
            "url": url,
            "content": content,
            "content_hash": digest,
            "sent_at": timestamp,
            "delivery_state": "confirmed",
        }
        if original is not None:
            row["original_messages"] = original
            row["original_messages_hash"] = content_sha256(
                json.dumps(original, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        if content_id:
            row["content_id"] = content_id
        rows.append(row)

    payload = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
    with _lock:
        dest.parent.mkdir(parents=True, exist_ok=True)
        with dest.open("a", encoding="utf-8") as fh:
            fh.write(payload)
    return len(rows)


DEFAULT_PATH = SENT_CONTENT_LEDGER
