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
# Process lock: launchd suppresses same-label overlap, but a manual catch-up
# can still race the scheduled job and double-hit the CLIProxyAPI model. mkdir lock with
# stale reclaim mirrors run_channels_guarded.sh.
#
# Overridable for testing: CHAT_DAILY_PY, CHAT_DAILY_DATA_DIR, CHAT_DAILY_ALERT_PROXY,
# CHAT_DAILY_GROWTH_LOCK_DIR.
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

# Lock protocol (empty/dead-pid TOCTOU-safe), same shape as channels:
#   1) mkdir LOCK_DIR — primary acquire; write pid immediately after
#   2) live owner (or young lock with missing pid) → "skip locked"
#   3) stale reclaim under exclusive LOCK_DIR.reclaim mutex
LOCK_DIR="${CHAT_DAILY_GROWTH_LOCK_DIR:-/tmp/chat-daily-tg-growth.lock}"
RECLAIM_DIR="${LOCK_DIR}.reclaim"

_growth_lock_release() {
  if [ -f "$LOCK_DIR/pid" ]; then
    _owner="$(cat "$LOCK_DIR/pid" 2>/dev/null || true)"
    if [ "$_owner" = "$$" ]; then
      rm -f "$LOCK_DIR/pid"
      rmdir "$LOCK_DIR" 2>/dev/null || true
    fi
  fi
}

_growth_lock_is_live() {
  local old_pid now mtime age
  if [ ! -d "$LOCK_DIR" ]; then
    return 1
  fi
  old_pid="$(cat "$LOCK_DIR/pid" 2>/dev/null || true)"
  case "$old_pid" in
    ''|*[!0-9]*)
      now="$(date +%s)"
      mtime="$(stat -f %m "$LOCK_DIR" 2>/dev/null || echo 0)"
      age=$((now - mtime))
      if [ "$age" -lt 60 ]; then
        return 0
      fi
      return 1
      ;;
  esac
  kill -0 "$old_pid" 2>/dev/null
}

_growth_lock_acquire() {
  if mkdir "$LOCK_DIR" 2>/dev/null; then
    echo $$ > "$LOCK_DIR/pid"
    return 0
  fi

  if _growth_lock_is_live; then
    old_pid="$(cat "$LOCK_DIR/pid" 2>/dev/null || true)"
    echo "$(date '+%F %T') skip locked: growth already running pid=${old_pid:-pending}" >> "$LOG"
    return 1
  fi

  if mkdir "$RECLAIM_DIR" 2>/dev/null; then
    if _growth_lock_is_live; then
      rmdir "$RECLAIM_DIR" 2>/dev/null || true
      old_pid="$(cat "$LOCK_DIR/pid" 2>/dev/null || true)"
      echo "$(date '+%F %T') skip locked: growth already running pid=${old_pid:-pending}" >> "$LOG"
      return 1
    fi
    rm -f "$LOCK_DIR/pid" 2>/dev/null || true
    if rmdir "$LOCK_DIR" 2>/dev/null; then
      if mkdir "$LOCK_DIR" 2>/dev/null; then
        echo $$ > "$LOCK_DIR/pid"
        rmdir "$RECLAIM_DIR" 2>/dev/null || true
        return 0
      fi
      rmdir "$RECLAIM_DIR" 2>/dev/null || true
      echo "$(date '+%F %T') skip locked: lost race to peer acquirer" >> "$LOG"
      return 1
    fi
    rmdir "$RECLAIM_DIR" 2>/dev/null || true
    echo "$(date '+%F %T') skip locked: stale lock dir not empty" >> "$LOG"
    return 1
  fi

  echo "$(date '+%F %T') skip locked: reclaim in progress" >> "$LOG"
  return 1
}

if ! _growth_lock_acquire; then
  # Soft skip: heartbeat 0 so monitors do not treat lock contention as failure.
  guard_heartbeat growth 0
  exit 0
fi
trap '_growth_lock_release' EXIT INT TERM

/usr/bin/caffeinate -is "$PY" "$PROJECT/run_daily.py" --growth-only --model sol
rc=$?
if [ "$rc" -ne 0 ]; then
  guard_notify "成长挖掘失败 exit=$rc，详见 $DATA_DIR/logs/growth-$(date +%F).log"
fi
guard_heartbeat growth "$rc"
exit "$rc"
