"""Tiny file-backed set of already-pushed channel message ids.

Makes the verbatim channel-card / private-media stage idempotent: a manual re-run,
a launchd wake-from-sleep catch-up, or a retry after a partial failure will skip
messages already delivered instead of re-pushing the whole window as duplicates.

Keys are "<chat_id>:<msg_id>" strings. The store is append-only and written
AFTER a successful send, so a crash re-tries the message next run rather than
dropping it. One key per line; loaded once per run.
"""
from __future__ import annotations

from pathlib import Path


class SeenStore:
    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()
        self._seen: set[str] = set()
        # Incremental channel polling asks for the same high-water mark once per
        # configured channel. Keep that lookup O(1) instead of rescanning every
        # historical seen key for every channel on every run.
        self._max_msg_ids: dict[str, int] = {}
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                key = line.strip()
                if key:
                    self._seen.add(key)
                    self._index_numeric_message_key(key)

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
        """Highest already-pushed msg_id for a channel (its high-water mark), or 0.
        Used by the incremental forwarder to fetch only newer messages."""
        return self._max_msg_ids.get(str(chat_id), 0)

    def __contains__(self, key: str) -> bool:
        return key in self._seen

    def add(self, key: str) -> None:
        if key in self._seen:
            return
        self._seen.add(key)
        self._index_numeric_message_key(key)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(key + "\n")
