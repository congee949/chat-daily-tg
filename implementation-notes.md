# Implementation Notes

## Design Decisions

- The official-X instant-push design uses five named account policies rather than a global keyword filter. Pure retweets are rejected deterministically before semantic classification, while quotes require substantive account-authored commentary.
- Codex quota operations from `thsottiaux` are treated as an OpenAI team source, not as an OpenAI corporate announcement. Completed grants/resets are eligible; polls, negation, conditional promises, and jokes are explicitly ineligible.
- OpenAI model announcements and OpenAIDevs API follow-ups share an event identity but are not automatically collapsed: a developer follow-up is delivered only when it adds structured technical facts.
- Event-index TTL is storage retention, not a universal dedup window. Quota resets use a 15-minute equivalence window and treat `again` / `another` or a new effective action as a new event, preserving multiple legitimate resets on the same day.
- X currently returns `claudeai` almost entirely as `TimelineTimelineModule` thread groups. The BWG GraphQL adapter now flattens module items before normalization; without this provider-level fix, the account appeared healthy but empty and its entitlement announcements were silently missed.
- The GraphQL adapter resolves immutable account IDs but normalized fallback tweets do not consistently expose author IDs. Production therefore verifies each configured official handle against GraphQL's immutable-ID cache/resolver before accepting either timeline source; inability to verify fails that account closed.

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

- The BWG production rollout implements the safe first stage of the official-X design: immutable account-ID validation, original-only gates, named deterministic policies, negative cases, seed-only activation for new accounts, and the per-event freshness windows (6h/24h over the 45-minute default). Structured classifier fallback, event-index dedup, specialized renderers, and thread idle bundling remain out of this deployment; enabling the five sources without the deterministic gates was rejected as unsafe.
- Deployed policy extends the design's account table: `claude_dev_original` additionally passes `quota_policy` entitlement announcements (with a promo-credit exclusion), because the 2026-07-18 "weekly limits 50% higher" announcement was posted on `ClaudeDevs` rather than `claudeai` and the table as designed missed it. `model_access` / `plan_entitlement` remain `claudeai`-only.
- BWG's pre-existing uncommitted `twitter_monitor.py` and `test_twitter_monitor.py` were used as the implementation baseline and preserved in timestamped server-side backups before deployment.

## Tradeoffs

- Ambiguous official originals may use a constrained structured classifier, but classifier failure is fail-closed. This favors notification precision over instant coverage; the underlying tweet remains available through the user's existing lookup tools.
- Quota events are sent immediately and suppress non-material self-replies, while launch threads use a short idle window. This accepts a small launch delay to avoid notification bursts without delaying time-sensitive resets.
- High-value event types override the monitor's current 45-minute freshness window (6 hours for resets/releases and 24 hours for entitlement changes), trading a bounded amount of catch-up traffic for resilience to short monitor outages.

- Health Auto Export `.hae` files are decoded with macOS `compression_tool`, avoiding a new Python dependency at the cost of a small subprocess overhead during the once-daily report.
- The PNG uses Pillow and system fonts instead of a browser renderer, keeping the unattended path lightweight. Telegram-native tables remain the source for exact values; the image is optimized for quick visual comparison.
- The existing Cloudflare relay configuration remains accepted for backward-compatible configuration loading, but the daily rich-digest path no longer depends on it.

## Open Questions

- Whether to build the deferred second stage (structured classifier fallback, event-index dedup, specialized renderers, thread idle bundling) is pending user decision; the deterministic first stage stands until a missed-event or noise incident argues otherwise.
