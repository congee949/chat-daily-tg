#!/bin/bash
# run_growth_weekly_guarded.sh — launchd wrapper for the growth weekly report (成长周报).
#
# Same guard scaffolding as run_channels_guarded.sh / run_growth_guarded.sh:
# venv preflight (the exit-127 silent-death mode fixed 2026-06-12) + macOS/
# Telegram alert on failure. StartCalendarInterval fires once a week
# (Saturday); no jitter/--skip-if-done needed, this is a single weekly compile.
#
# Overridable for testing: CHAT_DAILY_PY, CHAT_DAILY_DATA_DIR, CHAT_DAILY_ALERT_PROXY.
set -uo pipefail

PROJECT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA_DIR="${CHAT_DAILY_DATA_DIR:-$HOME/chat-daily}"
PY="${CHAT_DAILY_PY:-$PROJECT/.venv/bin/python}"
PROXY="${CHAT_DAILY_ALERT_PROXY:-http://127.0.0.1:1082}"
LOG="$DATA_DIR/logs/guard-growth-weekly-$(date +%F).log"
GUARD_TITLE="chat-daily-tg 成长周报守护"
mkdir -p "$DATA_DIR/logs"

source "$PROJECT/scripts/guard_common.sh"

if [ ! -x "$PY" ]; then
  guard_notify "venv python 缺失 ($PY)，成长周报未运行，请重建：cd $PROJECT && uv sync"
  guard_heartbeat growth-weekly 1
  exit 1
fi

guard_setup_env

/usr/bin/caffeinate -is "$PY" "$PROJECT/run_daily.py" --growth-weekly --model llm
rc=$?
if [ "$rc" -ne 0 ]; then
  guard_notify "成长周报失败 exit=$rc，详见 $DATA_DIR/logs/growth-weekly-$(date +%F).log"
fi
guard_heartbeat growth-weekly "$rc"
exit "$rc"
