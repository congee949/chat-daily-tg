# Implementation Notes

## Design Decisions

- Apple Watch data is read from the existing Health Auto Export iCloud `AutoSync` directory. The report uses the day after the covered chat date as its briefing date, analyzes the covered date's activity, and derives wake time from the sleep episode ending on the briefing date.
- Personal baselines use the prior 28 calendar days and require enough valid samples before a comparison is shown. Missing or stale exports are reported as unavailable, never coerced to zero.
- Raw-channel whole-post filtering is separate from line stripping: `exclude_patterns` suppresses a matching post, while `strip_patterns` only removes matching lines from a post that is still delivered.
- The health briefing now has one structured report model shared by the plain-text fallback, PNG chart, and Bot API 10.2 rich presentation. If the current morning sleep export is unavailable, the chart uses the most recent complete sleep episode and labels that older time window explicitly.
- Bot API 10.2 rich media is uploaded in the same multipart request and referenced with `tg://photo?id=...`. A health-card delivery marker prevents catch-up runs from duplicating a separately delivered fallback chart.
- To avoid repeating the same measurements three times, the normal rich message uses a qualitative visual, one sentence of interpretation, and a collapsed exact-data section. The native table is omitted when the chart is available.
- Exact sleep, workout, activity, and recent-comparison values are grouped into tables inside the collapsed details block. The explanatory baseline sentence is intentionally omitted from the reader-facing message.
- The chart expresses relative status only with symbols at the right edge of each bar: green up arrow, neutral equals sign, and red down arrow.
- For narrow mobile screens, the workout table contains only workout name and active energy. The activity comparison table contains only signed deltas from the recent median.
- The embedded health chart has no caption because the surrounding morning-report structure already explains its content.
- The chart footer does not repeat the sleep-record scope; that scope remains available only in the collapsed exact-data table (when a sleep episode exists). Because the PNG itself no longer discloses scope, the standalone-card fallback caption in run_daily.py now appends report.sleep_label.
- When the recent baseline is insufficient, the delta table shows "—" per row plus one note line ("近期基线样本不足（N 天，需 M 天）") replacing the old per-cell "样本 N 天，暂不比较" wording.

## Deviations

## Tradeoffs

- Health Auto Export `.hae` files are decoded with macOS `compression_tool`, avoiding a new Python dependency at the cost of a small subprocess overhead during the once-daily report.
- The PNG uses Pillow and system fonts instead of a browser renderer, keeping the unattended path lightweight. Telegram-native tables remain the source for exact values; the image is optimized for quick visual comparison.
- The existing Cloudflare relay configuration remains accepted for backward-compatible configuration loading, but the daily rich-digest path no longer depends on it.

## Open Questions
