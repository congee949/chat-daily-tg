#!/bin/bash
# run_growth_guarded.sh — launchd wrapper for the growth-mining job (成长挖掘).
#
# Same guard scaffolding as run_channels_guarded.sh: venv preflight (the
# exit-127 silent-death mode fixed 2026-06-12) + macOS/Telegram alert on
# failure, so a broken growth run doesn't just vanish. StartCalendarInterval
# fires 3x/day as catch-up retries; the job itself self-guards idempotency
# (growth_mined_days / growth_segments status), so a retry that finds the
# day already mined or the daily quota already sent is a cheap no-op.
#
# Overridable for testing: CHAT_DAILY_PY, CHAT_DAILY_DATA_DIR, CHAT_DAILY_ALERT_PROXY.
set -uo pipefail

PROJECT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA_DIR="${CHAT_DAILY_DATA_DIR:-$HOME/chat-daily}"
PY="${CHAT_DAILY_PY:-$PROJECT/.venv/bin/python}"
PROXY="${CHAT_DAILY_ALERT_PROXY:-http://127.0.0.1:1082}"
LOG="$DATA_DIR/logs/guard-growth-$(date +%F).log"
GUARD_TITLE="chat-daily-tg 成长挖掘守护"
mkdir -p "$DATA_DIR/logs"

source "$PROJECT/scripts/guard_common.sh"

if [ ! -x "$PY" ]; then
  guard_notify "venv python 缺失 ($PY)，成长挖掘未运行，请重建：cd $PROJECT && uv sync"
  guard_heartbeat growth 1
  exit 1
fi

guard_setup_env

/usr/bin/caffeinate -is "$PY" "$PROJECT/run_daily.py" --growth-only --model llm
rc=$?
if [ "$rc" -ne 0 ]; then
  guard_notify "成长挖掘失败 exit=$rc，详见 $DATA_DIR/logs/growth-$(date +%F).log"
fi
guard_heartbeat growth "$rc"
exit "$rc"
