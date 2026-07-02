#!/bin/bash
# run_bilibili_guarded.sh — launchd wrapper for the 6-hourly Bilibili digest.
#
# Same guard as the channel forwarder (venv preflight + macOS/Telegram alert,
# review finding #16). No jitter / no --skip-if-done: the digest is incremental
# and idempotent via the bvid SeenStore + 48h lookback, so a failed or
# slept-through run is simply caught up by the next one.
#
# NOTE: opencli itself must stay OFF the http proxy exports guard_setup_env
# sets — it talks to the LOCAL daemon (127.0.0.1, covered by NO_PROXY) and the
# browser bridge does its own networking, so no extra handling is needed here.
#
# Overridable for testing: CHAT_DAILY_PY, CHAT_DAILY_DATA_DIR, CHAT_DAILY_ALERT_PROXY.
set -uo pipefail

PROJECT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA_DIR="${CHAT_DAILY_DATA_DIR:-$HOME/chat-daily}"
PY="${CHAT_DAILY_PY:-$PROJECT/.venv/bin/python}"
PROXY="${CHAT_DAILY_ALERT_PROXY:-http://127.0.0.1:1082}"
LOG="$DATA_DIR/logs/guard-bilibili-$(date +%F).log"
GUARD_TITLE="chat-daily-tg B站守护"
mkdir -p "$DATA_DIR/logs"

source "$PROJECT/scripts/guard_common.sh"

if [ ! -x "$PY" ]; then
  guard_notify "venv python 缺失 ($PY)，B站 digest 未运行，请重建：cd $PROJECT && uv sync"
  exit 1
fi

guard_setup_env

/usr/bin/caffeinate -is "$PY" "$PROJECT/run_daily.py" --bilibili-only
rc=$?
if [ "$rc" -ne 0 ]; then
  guard_notify "B站 digest 失败 exit=$rc，详见 $DATA_DIR/logs/bilibili-$(date +%F).log"
fi
exit "$rc"
