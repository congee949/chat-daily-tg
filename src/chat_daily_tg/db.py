from __future__ import annotations
from dataclasses import dataclass, field, asdict
from hashlib import sha256
import json
import re
from pathlib import Path
from typing import Iterator, Literal


Category = Literal["invite_code", "bank_product", "activity", "misc"]
EntryType = Literal["permanent", "product", "activity"]
Status = Literal["alive", "likely_dead", "dead", "unknown"]


_NORMALIZE_RE = re.compile(r"[\s\W_]+", re.UNICODE)


def _normalize(s: str | None) -> str:
    if not s:
        return ""
    return _NORMALIZE_RE.sub("", s.lower())


def compute_fingerprint(title: str, url: str | None, category: str) -> str:
    """Stable identity of a permanent entry.

    URL is the strongest signal of opportunity identity — LLM title wording
    often drifts across runs ('X活动' vs 'X'). When URL exists, key on URL+category;
    otherwise fall back to normalized title + category.
    """
    if url and url.strip():
        key = f"url:{_normalize(url)}|{_normalize(category)}"
    else:
        key = f"title:{_normalize(title)}|{_normalize(category)}"
    return sha256(key.encode("utf-8")).hexdigest()


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

    def fingerprint(self) -> str:
        return compute_fingerprint(self.title, self.url, self.category)


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

    def _rewrite(self, entries: list[PermanentEntry]) -> None:
        self._ensure()
        with open(self.path, "w", encoding="utf-8") as f:
            for e in entries:
                f.write(json.dumps(asdict(e), ensure_ascii=False) + "\n")

    def _merge_one(self, existing: PermanentEntry, new: PermanentEntry) -> PermanentEntry:
        existing.mention_count += 1
        existing.last_mentioned_at = new.captured_at
        for attr in ("title", "content", "notes", "url", "source_sender", "source_group"):
            val = getattr(new, attr)
            if val:
                setattr(existing, attr, val)
        return existing

    def upsert_many(
        self, new_entries: list[PermanentEntry]
    ) -> list[tuple[Literal["inserted", "updated"], PermanentEntry]]:
        """Merge N entries in one read + one write pass.

        For each input, matches by fingerprint against current rows (and against
        rows already merged earlier in this batch).
        """
        if not new_entries:
            return []
        entries = list(self.read_all())
        by_fp: dict[str, PermanentEntry] = {e.fingerprint(): e for e in entries}
        results: list[tuple[Literal["inserted", "updated"], PermanentEntry]] = []
        for new in new_entries:
            fp = new.fingerprint()
            if fp in by_fp:
                merged = self._merge_one(by_fp[fp], new)
                results.append(("updated", merged))
            else:
                entries.append(new)
                by_fp[fp] = new
                results.append(("inserted", new))
        self._rewrite(entries)
        return results

    def upsert(
        self, entry: PermanentEntry
    ) -> tuple[Literal["inserted", "updated"], PermanentEntry]:
        """Single-entry convenience wrapper around upsert_many."""
        return self.upsert_many([entry])[0]

    def append(self, entry: PermanentEntry) -> None:
        """Deprecated: blind append. Use `upsert`/`upsert_many` instead."""
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
