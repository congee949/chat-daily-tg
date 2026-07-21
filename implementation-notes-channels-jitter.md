# Implementation Notes — channels wrapper jitter

## Design Decisions

- **Scope**: only `run_channels_guarded.sh` calls `guard_jitter_sleep`. Agent daily uses wake-gate for send timing; growth wrappers left untouched.
- **Placement**: jitter runs in the parent shell after `guard_setup_env`, before `caffeinate` + Python. AC + disablesleep already keeps the Mac awake under launchd; wrapping sleep in caffeinate is unnecessary complexity.
- **Defaults**: inclusive `[0, 900]` seconds (0–15 min), same awk `srand` approach as r4s `due_gate.sh`. Opt-out via `CHAT_DAILY_NO_JITTER=1`.
- **Logging**: append one line to guard-channels daily log (`jitter delay=…` or `jitter skipped`).

## Deviations

- Plan draft considered `caffeinate` wrapping both jitter and Python via nested `bash -c`; implemented the simpler parent-sleep + caffeinate-python form instead.

## Tradeoffs

- launchd StartCalendarInterval remains wall-clock; de-alignment is wrapper-only. task-monitor `grace_s` raised to 4500 so the extra 0–15 min does not false-alarm.

## Open Questions

- None.
