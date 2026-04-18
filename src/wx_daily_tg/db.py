from __future__ import annotations
from dataclasses import dataclass, field, asdict
import json
from pathlib import Path
from typing import Iterator, Literal


Category = Literal["invite_code", "bank_product", "activity", "misc"]
EntryType = Literal["permanent", "product", "activity"]
Status = Literal["alive", "likely_dead", "dead", "unknown"]


@dataclass
class PermanentEntry:
    id: str
    captured_at: str
    source_group: str
    source_sender: str
    category: Category
    type: EntryType
    title: str
    content: str
    url: str | None = None
    expires_at: str | None = None
    last_mentioned_at: str | None = None
    mention_count: int = 1
    status: Status = "alive"
    death_signal: str | None = None
    notes: str | None = None


@dataclass
class PermanentDB:
    path: Path

    def _ensure(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.touch()

    def read_all(self) -> Iterator[PermanentEntry]:
        if not self.path.exists():
            return
        with open(self.path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                data = json.loads(line)
                yield PermanentEntry(**data)

    def append(self, entry: PermanentEntry) -> None:
        self._ensure()
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(entry), ensure_ascii=False) + "\n")

    def find(self, entry_id: str) -> PermanentEntry | None:
        for e in self.read_all():
            if e.id == entry_id:
                return e
        return None

    def mark_status(
        self, entry_id: str, status: Status, death_signal: str | None = None
    ) -> bool:
        """Rewrite file with updated status for entry_id. Returns True if found."""
        entries = list(self.read_all())
        found = False
        for e in entries:
            if e.id == entry_id:
                e.status = status
                if death_signal is not None:
                    e.death_signal = death_signal
                found = True
        if found:
            with open(self.path, "w", encoding="utf-8") as f:
                for e in entries:
                    f.write(json.dumps(asdict(e), ensure_ascii=False) + "\n")
        return found
