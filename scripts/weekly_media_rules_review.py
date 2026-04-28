#!/usr/bin/env python
from __future__ import annotations

import argparse
from collections import Counter
from datetime import date, timedelta
import json
from pathlib import Path
import statistics


def main() -> int:
    args = parse_args()
    archive_root = args.archive_root.expanduser()
    output = args.output.expanduser()
    end = date.fromisoformat(args.end_date) if args.end_date else date.today()
    days = [end - timedelta(days=i) for i in range(args.days - 1, -1, -1)]

    media_rows = []
    vision_rows = []
    for day in days:
        day_dir = archive_root / f"{day:%Y}" / f"{day:%m}" / f"{day:%d}"
        media_rows.extend(_read_jsonl(day_dir / "media_candidates.jsonl", day=day.isoformat()))
        vision_rows.extend(_read_jsonl(day_dir / "vision.jsonl", day=day.isoformat()))

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_report(days, media_rows, vision_rows), encoding="utf-8")
    print(output)
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create a read-only weekly media/vision rules review.")
    parser.add_argument("--archive-root", type=Path, default=Path("~/chat-daily/archive"))
    parser.add_argument("--output", type=Path, default=Path("output/research/media-rules/latest.md"))
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--end-date", help="YYYY-MM-DD, defaults to today")
    return parser.parse_args()


def render_report(days: list[date], media_rows: list[dict], vision_rows: list[dict]) -> str:
    title = f"# Media Rules Review {days[0].isoformat()} to {days[-1].isoformat()}"
    lines = [
        title,
        "",
        "> Read-only report. Do not edit `media.py` or `vision.py` from this output alone; convert accepted false positives/negatives into fixtures first.",
        "",
        "## Summary",
        "",
        f"- Media candidates: {len(media_rows)}",
        f"- Vision analyses: {len(vision_rows)}",
        f"- Included by vision: {sum(1 for r in vision_rows if r.get('should_include_in_daily'))}",
        "",
    ]
    scores = [float(r.get("score", 0.0)) for r in media_rows]
    if scores:
        lines.extend([
            "## Candidate Score Distribution",
            "",
            f"- min: {min(scores):.2f}",
            f"- median: {statistics.median(scores):.2f}",
            f"- max: {max(scores):.2f}",
            "",
        ])

    reasons = Counter(str(r.get("reason", "")) for r in media_rows)
    if reasons:
        lines.extend(["## Top Candidate Reasons", ""])
        for reason, count in reasons.most_common(10):
            lines.append(f"- {count}x {reason}")
        lines.append("")

    lines.extend(["## High Score Candidates", ""])
    for row in sorted(media_rows, key=lambda r: float(r.get("score", 0.0)), reverse=True)[:20]:
        lines.append(_media_line(row))
    if not media_rows:
        lines.append("- No media candidates found.")
    lines.append("")

    lines.extend(["## Vision Included Examples", ""])
    included = [r for r in vision_rows if r.get("should_include_in_daily")]
    for row in included[:20]:
        candidate = row.get("candidate") or {}
        lines.append(
            f"- {row.get('_day')} {candidate.get('group_name', '')} "
            f"score={float(row.get('value_score', 0.0)):.2f}: {row.get('summary', '')}"
        )
    if not included:
        lines.append("- No included vision examples found.")
    lines.append("")

    lines.extend([
        "## Proposed Manual Review",
        "",
        "- Check top high-score candidates for false positives.",
        "- Check raw daily exports near images for missed low-score false negatives.",
        "- If changing keywords or thresholds, add a fixture/test first.",
        "- If changing the vision prompt, keep JSON output stable.",
        "",
    ])
    return "\n".join(lines)


def _read_jsonl(path: Path, *, day: str) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            data = json.loads(line)
            data["_day"] = day
            rows.append(data)
    return rows


def _media_line(row: dict) -> str:
    return (
        f"- {row.get('_day')} {row.get('platform', '')} / {row.get('group_name', '')} "
        f"/ {row.get('timestamp', '')} score={float(row.get('score', 0.0)):.2f} "
        f"{row.get('reason', '')}"
    )


if __name__ == "__main__":
    raise SystemExit(main())
