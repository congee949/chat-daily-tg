#!/bin/bash
# run_channels_guarded.sh — launchd wrapper for the 2-hourly channel forwarder.
#
# The channels plist used to call .venv/bin/python directly, re-opening the exact
# exit-127 silent failure fixed for the daily job on 2026-06-12: when .venv
# vanishes (uv prune/upgrade), launchd exits 127 before run_daily can log or
# alert, and the forwarder dies unnoticed. This wrapper reuses the daily guard
# (venv preflight + macOS/Telegram alert) so a broken forwarder is visible
# (review finding #16).
#
# After calendar launchd fire, guard_jitter_sleep waits a random 0–15 min so
# channel pushes are not wall-clock aligned (de-fingerprinting). Opt out with
# CHAT_DAILY_NO_JITTER=1 for manual catch-up / tests. No --skip-if-done: the
# forwarder is incremental and idempotent via its per-channel high-water mark.
#
# Overridable for testing: CHAT_DAILY_PY, CHAT_DAILY_DATA_DIR, CHAT_DAILY_ALERT_PROXY,
# CHAT_DAILY_NO_JITTER, CHAT_DAILY_JITTER_MIN_S, CHAT_DAILY_JITTER_MAX_S.
set -uo pipefail

PROJECT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA_DIR="${CHAT_DAILY_DATA_DIR:-$HOME/chat-daily}"
PY="${CHAT_DAILY_PY:-$PROJECT/.venv/bin/python}"
PROXY="${CHAT_DAILY_ALERT_PROXY:-http://127.0.0.1:1082}"
LOG="$DATA_DIR/logs/guard-channels-$(date +%F).log"
GUARD_TITLE="chat-daily-tg 频道守护"
mkdir -p "$DATA_DIR/logs"

source "$PROJECT/scripts/guard_common.sh"

if [ ! -x "$PY" ]; then
  guard_notify "venv python 缺失 ($PY)，频道转发未运行，请重建：cd $PROJECT && uv sync"
  guard_heartbeat channels 1
  exit 1
fi

guard_setup_env

# De-align from wall clock (0–15 min default); then caffeinate only the Python work.
# Parent sleep under launchd is fine while AC + disablesleep holds the machine awake.
guard_jitter_sleep
/usr/bin/caffeinate -is "$PY" "$PROJECT/run_daily.py" --channels-only
rc=$?
if [ "$rc" -ne 0 ]; then
  guard_notify "频道转发失败 exit=$rc，详见 $DATA_DIR/logs/channels-$(date +%F).log"
fi
guard_heartbeat channels "$rc"
exit "$rc"
