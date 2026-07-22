from __future__ import annotations
from dataclasses import dataclass, asdict
from hashlib import sha256
import re
from pathlib import Path
from typing import Iterator, Literal
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

from chat_daily_tg.sqlite_util import connect


Category = Literal["invite_code", "bank_product", "activity", "misc"]
EntryType = Literal["permanent", "product", "activity"]
Status = Literal["alive", "likely_dead", "dead", "unknown"]


_NORMALIZE_RE = re.compile(r"[\s\W_]+", re.UNICODE)

# Tracking / share params that do NOT change opportunity identity. Stripped
# before fingerprinting so the same activity forwarded with different utm/share
# tokens collapses to one entry (review finding #3).
_TRACKING_KEYS = {
    "from", "from_source", "_from", "spm", "scene", "fbclid", "gclid",
    "ref", "ref_src", "refer", "referer", "referrer", "src", "source",
    "wxshare", "weibo_id", "timestamp", "ts", "_t",
}


def _normalize(s: str | None) -> str:
    if not s:
        return ""
    return _NORMALIZE_RE.sub("", s.lower())


def _canonical_url(url: str) -> str:
    """Drop tracking params + fragment, lowercase host, strip trailing slash.

    Keeps identity-bearing query params (e.g. ?id=5) while removing utm_*/share*
    and the known tracking keys, so the same link shared with different campaign
    tags fingerprints identically.
    """
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return url.strip()
    if not parts.scheme and not parts.netloc:
        return url.strip()
    host = (parts.hostname or "").lower()
    netloc = f"{host}:{parts.port}" if parts.port else host
    path = parts.path.rstrip("/")
    kept = []
    for k, v in parse_qsl(parts.query, keep_blank_values=True):
        lk = k.lower()
        if lk.startswith("utm_") or lk.startswith("share") or lk in _TRACKING_KEYS:
            continue
        kept.append((k, v))
    kept.sort()
    return urlunsplit((parts.scheme.lower(), netloc, path, urlencode(kept), ""))


def compute_fingerprint(title: str, url: str | None, category: str) -> str:
    """Stable identity of a permanent entry.

    URL is the strongest signal of opportunity identity — LLM title wording
    often drifts across runs ('X活动' vs 'X'). When URL exists, key on a
    canonicalized URL + category; otherwise fall back to normalized title.
    """
    if url and url.strip():
        key = f"url:{_normalize(_canonical_url(url))}|{_normalize(category)}"
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


_FIELDS = (
    "id", "captured_at", "source_group", "source_sender", "category", "type",
    "title", "content", "url", "expires_at", "last_mentioned_at",
    "mention_count", "status", "death_signal", "notes",
)


def _row_to_entry(row) -> PermanentEntry:
    return PermanentEntry(**{k: row[k] for k in _FIELDS})


@dataclass
class PermanentDB:
    path: Path

    def _conn(self):
        return connect(self.path)

    def read_all(self) -> Iterator[PermanentEntry]:
        conn = self._conn()
        try:
            for row in conn.execute("SELECT * FROM permanent ORDER BY rowid"):
                yield _row_to_entry(row)
        finally:
            conn.close()

    def _write(self, conn, entry: PermanentEntry) -> None:
        data = asdict(entry)
        data["fingerprint"] = entry.fingerprint()
        cols = list(_FIELDS) + ["fingerprint"]
        placeholders = ", ".join(f":{c}" for c in cols)
        conn.execute(
            f"INSERT INTO permanent ({', '.join(cols)}) VALUES ({placeholders}) "
            "ON CONFLICT(id) DO UPDATE SET "
            + ", ".join(f"{c}=excluded.{c}" for c in cols if c != "id"),
            data,
        )

    def upsert_many(
        self, new_entries: list[PermanentEntry]
    ) -> list[tuple[Literal["inserted", "updated"], PermanentEntry]]:
        """Merge N entries in one transaction, matching by SQLite's fingerprint key.

        The former implementation first loaded every historic row into Python
        and rebuilt its fingerprint before processing a batch.  That turned a
        one-item daily update into O(total history).  ``fingerprint`` is already
        a UNIQUE database constraint, so use it as the conflict arbiter and
        read back only the rows touched by this call.
        """
        if not new_entries:
            return []
        conn = self._conn()
        try:
            results: list[tuple[Literal["inserted", "updated"], PermanentEntry]] = []
            with conn:
                for new in new_entries:
                    fingerprint = new.fingerprint()
                    existed = conn.execute(
                        "SELECT 1 FROM permanent WHERE fingerprint = ?", (fingerprint,)
                    ).fetchone() is not None
                    data = asdict(new)
                    data["fingerprint"] = fingerprint
                    cols = list(_FIELDS) + ["fingerprint"]
                    placeholders = ", ".join(f":{c}" for c in cols)
                    conn.execute(
                        f"INSERT INTO permanent ({', '.join(cols)}) VALUES ({placeholders}) "
                        "ON CONFLICT(fingerprint) DO UPDATE SET "
                        "mention_count = permanent.mention_count + 1, "
                        "last_mentioned_at = excluded.captured_at, "
                        "title = COALESCE(NULLIF(excluded.title, ''), permanent.title), "
                        "content = COALESCE(NULLIF(excluded.content, ''), permanent.content), "
                        "url = COALESCE(NULLIF(excluded.url, ''), permanent.url), "
                        "notes = COALESCE(NULLIF(excluded.notes, ''), permanent.notes), "
                        "source_sender = COALESCE(NULLIF(excluded.source_sender, ''), permanent.source_sender), "
                        "source_group = COALESCE(NULLIF(excluded.source_group, ''), permanent.source_group)",
                        data,
                    )
                    row = conn.execute(
                        "SELECT * FROM permanent WHERE fingerprint = ?", (fingerprint,)
                    ).fetchone()
                    assert row is not None
                    results.append(("updated" if existed else "inserted", _row_to_entry(row)))
            return results
        finally:
            conn.close()

    def upsert(
        self, entry: PermanentEntry
    ) -> tuple[Literal["inserted", "updated"], PermanentEntry]:
        """Single-entry convenience wrapper around upsert_many."""
        return self.upsert_many([entry])[0]

    def append(self, entry: PermanentEntry) -> None:
        """Deprecated: blind insert (no fingerprint merge). Use `upsert` instead."""
        conn = self._conn()
        try:
            with conn:
                self._write(conn, entry)
        finally:
            conn.close()

    def find(self, entry_id: str) -> PermanentEntry | None:
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT * FROM permanent WHERE id = ?", (entry_id,)
            ).fetchone()
            return _row_to_entry(row) if row else None
        finally:
            conn.close()

    def mark_status(
        self, entry_id: str, status: Status, death_signal: str | None = None
    ) -> bool:
        """Update status (and optional death_signal) for entry_id. True if found."""
        conn = self._conn()
        try:
            with conn:
                if death_signal is not None:
                    cur = conn.execute(
                        "UPDATE permanent SET status = ?, death_signal = ? WHERE id = ?",
                        (status, death_signal, entry_id),
                    )
                else:
                    cur = conn.execute(
                        "UPDATE permanent SET status = ? WHERE id = ?",
                        (status, entry_id),
                    )
                return cur.rowcount > 0
        finally:
            conn.close()
