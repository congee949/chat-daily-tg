"""One-shot: compress existing permanent.jsonl by fingerprint.

For each bucket:
  - keep the latest captured_at row's content/title/notes/url as canonical
  - set captured_at = earliest in bucket (original first-sight)
  - set last_mentioned_at = latest captured_at
  - mention_count = size of bucket
  - id = f"{first_captured_date}-{fp[:8]}"

Bucket key is the strict fingerprint (url+category, falling back to title+category).
When URL is absent and LLM titles drift across runs, a second pass groups by
(first-6-CJK-chars of title, category) to absorb near-duplicates into the same
opportunity row. The canonical fingerprint of the merged row is preserved.

Also regenerates permanent.md afterward.
"""
from __future__ import annotations
from collections import defaultdict
from dataclasses import asdict
import re

from chat_daily_tg.db import PermanentDB, PermanentEntry, compute_fingerprint
from chat_daily_tg.paths import PERMANENT_JSONL, PERMANENT_MD
from chat_daily_tg.permanent_md import regenerate_permanent_md


_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_SIM_THRESHOLD = 0.65


def _norm(s: str) -> str:
    from chat_daily_tg.db import _NORMALIZE_RE
    return _NORMALIZE_RE.sub("", (s or "").lower())


def _title_similarity(a: str, b: str) -> float:
    from difflib import SequenceMatcher
    return SequenceMatcher(None, _norm(a), _norm(b)).ratio()


def _merge_similar(rows: list[PermanentEntry]) -> list[PermanentEntry]:
    """Greedy union-find by (category, title similarity)."""
    groups: list[list[PermanentEntry]] = []
    for row in rows:
        placed = False
        for g in groups:
            head = g[0]
            if head.category != row.category:
                continue
            if _title_similarity(head.title, row.title) >= _SIM_THRESHOLD:
                g.append(row)
                placed = True
                break
        if not placed:
            groups.append([row])
    out: list[PermanentEntry] = []
    for g in groups:
        g.sort(key=lambda r: r.captured_at)
        earliest, latest = g[0], g[-1]
        latest.captured_at = earliest.captured_at
        latest.last_mentioned_at = latest.captured_at if len(g) == 1 else g[-1].captured_at
        latest.mention_count = sum(r.mention_count for r in g)
        out.append(latest)
    return out


def main() -> None:
    db = PermanentDB(PERMANENT_JSONL)
    buckets: dict[str, list[PermanentEntry]] = defaultdict(list)
    for e in db.read_all():
        buckets[e.fingerprint()].append(e)

    merged: list[PermanentEntry] = []
    for fp, rows in buckets.items():
        rows_sorted = sorted(rows, key=lambda r: r.captured_at)
        earliest, latest = rows_sorted[0], rows_sorted[-1]
        first_date = earliest.captured_at[:10]
        new_id = f"{first_date}-{fp[:8]}"
        merged.append(
            PermanentEntry(
                id=new_id,
                captured_at=earliest.captured_at,
                last_mentioned_at=latest.captured_at if len(rows) > 1 else None,
                mention_count=len(rows),
                source_group=latest.source_group,
                source_sender=latest.source_sender,
                category=latest.category,
                type=latest.type,
                title=latest.title,
                content=latest.content,
                url=latest.url,
                expires_at=latest.expires_at,
                status=latest.status,
                death_signal=latest.death_signal,
                notes=latest.notes,
            )
        )

    # Second pass: collapse near-duplicate rows whose URLs are absent and titles
    # are similar (SequenceMatcher ratio ≥ 0.65 within the same category).
    urlless = [e for e in merged if not (e.url and e.url.strip())]
    withurl = [e for e in merged if (e.url and e.url.strip())]
    final = list(withurl) + _merge_similar(urlless)
    final.sort(key=lambda r: r.captured_at)
    db._rewrite(final)
    print(
        f"compressed {sum(len(v) for v in buckets.values())} rows → "
        f"{len(merged)} fp-unique → {len(final)} after loose title merge"
    )
    merged = final
    regenerate_permanent_md(PERMANENT_JSONL, PERMANENT_MD)
    print(f"regenerated {PERMANENT_MD}")


if __name__ == "__main__":
    main()
