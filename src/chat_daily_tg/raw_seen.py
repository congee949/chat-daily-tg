"""Tiny file-backed set of already-pushed channel message ids.

Makes the verbatim channel-card / private-media stage idempotent: a manual re-run,
a launchd wake-from-sleep catch-up, or a retry after a partial failure will skip
messages already delivered instead of re-pushing the whole window as duplicates.

Keys are "<chat_id>:<msg_id>" strings. The store is append-only and written
AFTER a successful send, so a crash re-tries the message next run rather than
dropping it. One key per line; loaded once per run.

Soft failures (network/API that are NOT terminal ambiguous) may leave a hole
below a later successful id. Without tracking holes, ``max_msg_id`` would jump
past the failed id and the incremental forwarder would never re-fetch it.
Holes live in a sibling ``*.holes`` file and cap the high-water mark just below
the lowest hole for that channel.
"""
from __future__ import annotations

import logging
import os
from collections.abc import Iterable
from pathlib import Path

log = logging.getLogger(__name__)


def _usable_seen_key(key: str) -> bool:
    """Reject truncated leftovers such as ``bilibi`` after an ENOSPC write."""
    return bool(key) and ":" in key


def _read_complete_lines(path: Path) -> list[str]:
    """Read keys, dropping a trailing partial line (file not newline-terminated)."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return []
    if raw and not raw.endswith("\n"):
        raw = raw.rsplit("\n", 1)[0] if "\n" in raw else ""
    return [line.strip() for line in raw.splitlines() if line.strip()]


def _append_line(path: Path, key: str) -> bool:
    """Append one key with fsync. False on any OSError (including ENOSPC)."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(key + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        return True
    except OSError:
        return False


def mark_after_send(seen: "SeenStore", key: str) -> bool:
    """Write-after-send helper: never raise; False means persist failed."""
    try:
        return bool(seen.add(key))
    except OSError as e:
        log.error("seen persist raised for %s: %s", key, e)
        return False


class SeenStore:
    def __init__(self, path: str | Path, overflow_dir: str | Path | None = None):
        self.path = Path(path).expanduser()
        self.holes_path = self.path.with_name(self.path.name + ".holes")
        self.overflow_path = (
            Path(overflow_dir).expanduser() / self.path.name if overflow_dir else None
        )
        self._seen: set[str] = set()
        # Incremental channel polling asks for the same high-water mark once per
        # configured channel. Keep that lookup O(1) instead of rescanning every
        # historical seen key for every channel on every run.
        self._max_msg_ids: dict[str, int] = {}
        self._holes: dict[str, set[int]] = {}
        for source in (self.path, self.overflow_path):
            if source is None or not source.exists():
                continue
            for key in _read_complete_lines(source):
                if not _usable_seen_key(key):
                    continue
                self._seen.add(key)
                self._index_numeric_message_key(key)
        if self.holes_path.exists():
            for key in _read_complete_lines(self.holes_path):
                chat_id, separator, raw_msg_id = key.rpartition(":")
                if not separator or not chat_id:
                    continue
                try:
                    msg_id = int(raw_msg_id)
                except ValueError:
                    continue
                self._holes.setdefault(chat_id, set()).add(msg_id)

    @staticmethod
    def key(chat_id: str | int, msg_id: int) -> str:
        return f"{chat_id}:{msg_id}"

    def _index_numeric_message_key(self, key: str) -> None:
        """Index channel-style ``chat_id:numeric_msg_id`` keys.

        The same append-only file also stores Bilibili/YouTube identifiers whose
        suffix is not numeric. Those keys remain valid membership entries but do
        not participate in a Telegram channel high-water mark.
        """
        chat_id, separator, raw_msg_id = key.rpartition(":")
        if not separator or not chat_id:
            return
        try:
            msg_id = int(raw_msg_id)
        except ValueError:
            return
        previous = self._max_msg_ids.get(chat_id, 0)
        if msg_id > previous:
            self._max_msg_ids[chat_id] = msg_id

    def max_msg_id(self, chat_id: str | int) -> int:
        """Highest safe high-water mark for incremental fetch, or 0.

        Caps at ``min(hole) - 1`` when soft-failed message ids sit below later
        successful deliveries, so the next run re-includes the hole.
        """
        chat = str(chat_id)
        base = self._max_msg_ids.get(chat, 0)
        holes = self._holes.get(chat)
        if holes:
            return min(base, min(holes) - 1)
        return base

    def __contains__(self, key: str) -> bool:
        return key in self._seen

    def absorb(self, keys: Iterable[str]) -> int:
        """In-memory merge (ledger hydrate). Does not persist. Returns new keys."""
        added = 0
        for raw in keys:
            key = (raw or "").strip()
            if not _usable_seen_key(key) or key in self._seen:
                continue
            self._seen.add(key)
            self._index_numeric_message_key(key)
            added += 1
        return added

    def add(self, key: str) -> bool:
        """Remember ``key``. Persist to disk; overflow on ENOSPC. Never raises.

        Returns True if the key is durable (already present, primary write, or
        overflow write). In-memory membership is always updated first so the
        rest of this process will not resend even if persist fails.
        """
        if key in self._seen:
            chat_id, separator, raw_msg_id = key.rpartition(":")
            if separator:
                try:
                    self.clear_hole(chat_id, int(raw_msg_id))
                except (ValueError, OSError):
                    pass
            return True
        self._seen.add(key)
        self._index_numeric_message_key(key)
        persisted = _append_line(self.path, key)
        if not persisted and self.overflow_path is not None:
            persisted = _append_line(self.overflow_path, key)
            if persisted:
                log.warning("seen primary persist failed; wrote overflow %s",
                            self.overflow_path)
        if not persisted:
            log.error("seen persist failed for %s (primary%s)",
                      key, " and overflow" if self.overflow_path else "")
        chat_id, separator, raw_msg_id = key.rpartition(":")
        if separator:
            try:
                self.clear_hole(chat_id, int(raw_msg_id))
            except (ValueError, OSError):
                pass
        return persisted

    def add_hole(self, chat_id: str | int, msg_id: int) -> None:
        """Record a soft-failed id so the high-water mark cannot jump past it."""
        chat = str(chat_id)
        key = self.key(chat, msg_id)
        if key in self._seen:
            return
        bucket = self._holes.setdefault(chat, set())
        if msg_id in bucket:
            return
        bucket.add(msg_id)
        self.holes_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.holes_path, "a", encoding="utf-8") as f:
            f.write(key + "\n")

    def clear_hole(self, chat_id: str | int, msg_id: int) -> None:
        chat = str(chat_id)
        bucket = self._holes.get(chat)
        if not bucket or msg_id not in bucket:
            return
        bucket.discard(msg_id)
        if not bucket:
            self._holes.pop(chat, None)
        lines: list[str] = []
        for c, ids in sorted(self._holes.items()):
            for mid in sorted(ids):
                lines.append(f"{c}:{mid}")
        if lines:
            self.holes_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        elif self.holes_path.exists():
            self.holes_path.write_text("", encoding="utf-8")
