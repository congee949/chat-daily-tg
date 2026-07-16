# Implementation Notes

## Design Decisions

- Apple Watch data is read from the existing Health Auto Export iCloud `AutoSync` directory. The report uses the day after the covered chat date as its briefing date, analyzes the covered date's activity, and derives wake time from the sleep episode ending on the briefing date.
- Personal baselines use the prior 28 calendar days and require enough valid samples before a comparison is shown. Missing or stale exports are reported as unavailable, never coerced to zero.
- Raw-channel whole-post filtering is separate from line stripping: `exclude_patterns` suppresses a matching post, while `strip_patterns` only removes matching lines from a post that is still delivered.

## Deviations

## Tradeoffs

- Health Auto Export `.hae` files are decoded with macOS `compression_tool`, avoiding a new Python dependency at the cost of a small subprocess overhead during the once-daily report.

## Open Questions
