#!/bin/bash
# run_daily_guarded.sh — launchd wrapper for the daily report.
#
# Detects the two silent-failure modes seen on 2026-06-12 and alerts on them, so a
# broken run is noticed the same morning instead of as "why no report today":
#   1. missing .venv/bin/python  → launchd exits 127 BEFORE run_daily can log/notify
#   2. any non-zero run_daily exit (push failure, etc.)
#
# Alert path: macOS notification (offline, always fires) + best-effort Telegram
# message over the local http proxy. Does NOT touch Shadowrocket. Reads the bot
# token from the same ~/chat-daily/.env the pipeline uses.
#
# Overridable for testing: CHAT_DAILY_PY, CHAT_DAILY_DATA_DIR, CHAT_DAILY_ALERT_PROXY.
set -uo pipefail

PROJECT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA_DIR="${CHAT_DAILY_DATA_DIR:-$HOME/chat-daily}"
PY="${CHAT_DAILY_PY:-$PROJECT/.venv/bin/python}"
PROXY="${CHAT_DAILY_ALERT_PROXY:-http://127.0.0.1:1082}"
LOG="$DATA_DIR/logs/guard-$(date +%F).log"
GUARD_TITLE="chat-daily-tg 守护"
mkdir -p "$DATA_DIR/logs"

source "$PROJECT/scripts/guard_common.sh"

# Pre-flight: the exact failure that ate 2026-06-12 — venv python vanished.
if [ ! -x "$PY" ]; then
  guard_notify "venv python 缺失 ($PY)，今日日报未运行，请重建：cd $PROJECT && uv sync"
  guard_heartbeat daily 1
  exit 1
fi

# Make Telegram/DeepSeek reachable for the Python run + enable in-Python TG alerts.
guard_setup_env

# Normal run — caffeinate holds off sleep, same as the original plist.
# --wait-for-wake gates delivery on the Watch sleep episode syncing in (5-min
# polls from 07:05, forced delivery at the 13:00 deadline), which also replaces
# the old 0–15 min cosmetic jitter: the send moment now follows the user's
# actual wake-up, not the trigger minute.
# CHAT_DAILY_WAKE_DEADLINE (optional): injected by schedule.py into the agent
# plist's EnvironmentVariables. Absent → run_daily's built-in 13:00 default.
/usr/bin/caffeinate -is "$PY" "$PROJECT/run_daily.py" --skip-if-done --wait-for-wake \
  ${CHAT_DAILY_WAKE_DEADLINE:+--wake-deadline "$CHAT_DAILY_WAKE_DEADLINE"}
rc=$?
if [ "$rc" -ne 0 ]; then
  guard_notify "日报运行失败 exit=$rc，详见 $DATA_DIR/logs/$(date +%F).log"
fi
guard_heartbeat daily "$rc"
exit "$rc"
