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
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                k = line.strip()
                if k:
                    self._seen.add(k)

    @staticmethod
    def key(chat_id: str | int, msg_id: int) -> str:
        return f"{chat_id}:{msg_id}"

    def max_msg_id(self, chat_id: str | int) -> int:
        """Highest already-pushed msg_id for a channel (its high-water-mark), or 0.
        Used by the incremental forwarder to fetch only newer messages."""
        prefix = f"{chat_id}:"
        best = 0
        for k in self._seen:
            if k.startswith(prefix):
                try:
                    best = max(best, int(k[len(prefix):]))
                except ValueError:
                    continue
        return best

    def __contains__(self, key: str) -> bool:
        return key in self._seen

    def add(self, key: str) -> None:
        if key in self._seen:
            return
        self._seen.add(key)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(key + "\n")
