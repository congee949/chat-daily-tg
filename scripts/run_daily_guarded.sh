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
mkdir -p "$DATA_DIR/logs"

notify() {
  local msg="$1"
  # Offline fallback first — never depends on network/proxy being up.
  osascript -e "display notification \"${msg//\"/ }\" with title \"chat-daily-tg 守护\"" 2>/dev/null || true
  # Best-effort Telegram alert over the http proxy (TG is unreachable direct here).
  if [ -f "$DATA_DIR/.env" ]; then
    local tok cid
    tok=$(grep -m1 '^TG_BOT_TOKEN=' "$DATA_DIR/.env" | cut -d= -f2-)
    cid=$(grep -m1 '^TG_CHAT_ID=' "$DATA_DIR/.env" | cut -d= -f2-)
    if [ -n "$tok" ] && [ -n "$cid" ]; then
      curl -s --max-time 15 --proxy "$PROXY" \
        "https://api.telegram.org/bot${tok}/sendMessage" \
        --data-urlencode "chat_id=${cid}" \
        --data-urlencode "text=⚠️ chat-daily-tg 守护: ${msg}" >/dev/null 2>&1 || true
    fi
  fi
  echo "$(date '+%F %T') ALERT: $msg" >> "$LOG"
}

# Pre-flight: the exact failure that ate 2026-06-12 — venv python vanished.
if [ ! -x "$PY" ]; then
  notify "venv python 缺失 ($PY)，今日日报未运行，请重建：cd $PROJECT && uv sync"
  exit 1
fi

# Random jitter (0–15 min) so the daily run isn't at a perfectly fixed minute.
# NOTE: cosmetic for this setup — the run reads the LOCAL WeChat DB and delivers to
# Telegram, so WeChat's servers never observe it; jitter would only matter if we ever
# posted back INTO WeChat. Kept < 15 min so it never collides with the 9:00/13:00 catch-up.
# Skip the wait for manual/test runs: CHAT_DAILY_NO_JITTER=1.
if [ "${CHAT_DAILY_NO_JITTER:-0}" != "1" ]; then
  sleep $(( RANDOM % 900 ))
fi

# Normal run — caffeinate holds off sleep, same as the original plist.
/usr/bin/caffeinate -is "$PY" "$PROJECT/run_daily.py" --skip-if-done
rc=$?
if [ "$rc" -ne 0 ]; then
  notify "日报运行失败 exit=$rc，详见 $DATA_DIR/logs/$(date +%F).log"
fi
exit "$rc"
