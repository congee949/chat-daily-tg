"""Append-only ledger: Telegram message_id → canonical media URL.

Written after a subscription card is successfully sent (write-after-send).
Podcast4bot reads the same file on 👍 reactions to resolve a URL without
needing message text in the reaction update.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from chat_daily_tg.paths import MEDIA_SENT_LEDGER, STATE_DIR

log = logging.getLogger(__name__)

_lock = threading.Lock()
_index: dict[tuple[int, int], dict[str, Any]] | None = None
_index_path: Path | None = None
_index_signature: tuple[int, int, int, int, int] | None = None


def _file_signature(path: Path) -> tuple[int, int, int, int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _coerce_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def append_sent(
    *,
    chat_id: int | str,
    message_id: int | str,
    url: str,
    producer: str,
    thread_id: int | str | None = None,
    content_id: str | None = None,
    path: Path | None = None,
    ts: str | None = None,
) -> dict[str, Any] | None:
    """Append one ledger row after a successful send. Returns the row, or None if skipped."""
    cid = _coerce_int(chat_id)
    mid = _coerce_int(message_id)
    if cid is None or mid is None or not url:
        log.warning("sent_ledger skip: bad chat_id/message_id/url (%r, %r, %r)",
                    chat_id, message_id, url)
        return None
    row: dict[str, Any] = {
        "chat_id": cid,
        "message_id": mid,
        "url": url,
        "producer": producer,
        "ts": ts or _now_iso(),
    }
    tid = _coerce_int(thread_id)
    if tid is not None:
        row["thread_id"] = tid
    if content_id:
        row["id"] = content_id

    dest = Path(path) if path is not None else MEDIA_SENT_LEDGER
    line = json.dumps(row, ensure_ascii=False) + "\n"
    with _lock:
        dest.parent.mkdir(parents=True, exist_ok=True)
        with dest.open("a", encoding="utf-8") as fh:
            fh.write(line)
        # An external writer may have appended since the last lookup. Do not
        # mark those unread rows as cached by merely advancing the file size.
        global _index, _index_signature
        if _index is not None and _index_path == dest.resolve():
            _index = None
            _index_signature = None
    return row


def append_message_ids(
    message_ids: list[int] | int | None,
    *,
    chat_id: int | str,
    url: str,
    producer: str,
    thread_id: int | str | None = None,
    content_id: str | None = None,
    path: Path | None = None,
) -> int:
    """Write one row per message_id (album / multi-chunk cards). Returns rows written."""
    if message_ids is None:
        return 0
    if isinstance(message_ids, int):
        ids = [message_ids]
    else:
        ids = [m for m in message_ids if m is not None]
    n = 0
    for mid in ids:
        if append_sent(
            chat_id=chat_id,
            message_id=mid,
            url=url,
            producer=producer,
            thread_id=thread_id,
            content_id=content_id,
            path=path,
        ) is not None:
            n += 1
    return n


def _load_index(path: Path) -> dict[tuple[int, int], dict[str, Any]]:
    index: dict[tuple[int, int], dict[str, Any]] = {}
    if not path.exists():
        return index
    text = path.read_text(encoding="utf-8")
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        cid = _coerce_int(row.get("chat_id"))
        mid = _coerce_int(row.get("message_id"))
        if cid is None or mid is None or not row.get("url"):
            continue
        index[(cid, mid)] = row
    return index


def lookup(
    chat_id: int | str,
    message_id: int | str,
    *,
    path: Path | None = None,
) -> dict[str, Any] | None:
    """Return the latest ledger row for (chat_id, message_id), or None."""
    cid = _coerce_int(chat_id)
    mid = _coerce_int(message_id)
    if cid is None or mid is None:
        return None
    dest = Path(path) if path is not None else MEDIA_SENT_LEDGER
    global _index, _index_path, _index_signature
    with _lock:
        resolved = dest.resolve() if dest.exists() else dest
        signature = _file_signature(dest)
        if (
            _index is None
            or _index_path != resolved
            or signature is None
            or _index_signature != signature
        ):
            try:
                loaded = _load_index(dest)
            except OSError as exc:
                log.warning("sent_ledger read failed %s: %s", dest, exc)
                _index = None
                _index_signature = None
                return None
            if signature is not None and _file_signature(dest) == signature:
                _index = loaded
                _index_path = resolved
                _index_signature = signature
            else:
                # Return this read without pretending it was a stable snapshot.
                _index = None
                _index_signature = None
                return loaded.get((cid, mid))
        return _index.get((cid, mid))


def content_ids(*, producer: str | None = None, path: Path | None = None) -> set[str]:
    """Return persisted ``id`` values, skipping a truncated trailing line."""
    dest = Path(path) if path is not None else MEDIA_SENT_LEDGER
    ids: set[str] = set()
    if not dest.exists():
        return ids
    try:
        text = dest.read_text(encoding="utf-8")
    except OSError as e:
        log.warning("sent_ledger content_ids read failed %s: %s", dest, e)
        return ids
    if text and not text.endswith("\n"):
        text = text.rsplit("\n", 1)[0] if "\n" in text else ""
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        cid = row.get("id")
        if not isinstance(cid, str) or not cid:
            continue
        if producer and row.get("producer") != producer:
            continue
        ids.add(cid)
    return ids


def clear_cache() -> None:
    """Test helper: drop in-memory index."""
    global _index, _index_path, _index_signature
    with _lock:
        _index = None
        _index_path = None
        _index_signature = None


DEFAULT_PATH = MEDIA_SENT_LEDGER
STATE_DIR_PATH = STATE_DIR
