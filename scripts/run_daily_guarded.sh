#!/bin/bash
# run_daily_guarded.sh — launchd wrapper for the daily report.
#
# Detects the two silent-failure modes seen on 2026-06-12 and alerts on them, so a
# broken run is noticed the same morning instead of as "why no report today":
#   1. missing .venv/bin/python  → launchd exits 127 BEFORE run_daily can log/notify
#   2. any non-zero run_daily exit (push failure, etc.)
#
# Process lock: launchd suppresses same-label overlap, but a manual catch-up can
# still race the scheduled agent and double-load qwenproxy/summary. mkdir lock
# with stale reclaim mirrors run_channels_guarded.sh.
#
# Alert path: macOS notification (offline, always fires) + best-effort Telegram
# message over the local http proxy. Does NOT touch Shadowrocket. Reads the bot
# token from the same ~/chat-daily/.env the pipeline uses.
#
# Overridable for testing: CHAT_DAILY_PY, CHAT_DAILY_DATA_DIR, CHAT_DAILY_ALERT_PROXY,
# CHAT_DAILY_AGENT_LOCK_DIR.
set -uo pipefail

PROJECT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA_DIR="${CHAT_DAILY_DATA_DIR:-$HOME/chat-daily}"
PY="${CHAT_DAILY_PY:-$PROJECT/.venv/bin/python}"
PROXY="${CHAT_DAILY_ALERT_PROXY:-http://127.0.0.1:1082}"
LOG="$DATA_DIR/logs/guard-$(date +%F).log"
# Freeze the report day before a long run crosses midnight. The app uses this
# same day for its log and completion marker.
REPORT_DATE="$(date -v-1d +%F)"
REPORT_LOG="$DATA_DIR/logs/$REPORT_DATE.log"
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

# Lock protocol (empty/dead-pid TOCTOU-safe), same shape as channels.
LOCK_DIR="${CHAT_DAILY_AGENT_LOCK_DIR:-/tmp/chat-daily-tg-agent.lock}"
RECLAIM_DIR="${LOCK_DIR}.reclaim"

_agent_lock_release() {
  if [ -f "$LOCK_DIR/pid" ]; then
    _owner="$(cat "$LOCK_DIR/pid" 2>/dev/null || true)"
    if [ "$_owner" = "$$" ]; then
      rm -f "$LOCK_DIR/pid"
      rmdir "$LOCK_DIR" 2>/dev/null || true
    fi
  fi
}

_agent_lock_is_live() {
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

_agent_lock_acquire() {
  if mkdir "$LOCK_DIR" 2>/dev/null; then
    echo $$ > "$LOCK_DIR/pid"
    return 0
  fi

  if _agent_lock_is_live; then
    old_pid="$(cat "$LOCK_DIR/pid" 2>/dev/null || true)"
    echo "$(date '+%F %T') skip locked: agent already running pid=${old_pid:-pending}" >> "$LOG"
    return 1
  fi

  if mkdir "$RECLAIM_DIR" 2>/dev/null; then
    if _agent_lock_is_live; then
      rmdir "$RECLAIM_DIR" 2>/dev/null || true
      old_pid="$(cat "$LOCK_DIR/pid" 2>/dev/null || true)"
      echo "$(date '+%F %T') skip locked: agent already running pid=${old_pid:-pending}" >> "$LOG"
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

if ! _agent_lock_acquire; then
  guard_heartbeat daily 0
  exit 0
fi
trap '_agent_lock_release' EXIT INT TERM

# Normal run — caffeinate holds off sleep, same as the original plist.
# --wait-for-wake probes once for this morning's Watch sleep episode so the
# health card can use a real wake time when already synced; if sleep data is
# missing/unreadable, the digest delivers immediately (no 13:00 spin).
# CHAT_DAILY_WAKE_DEADLINE kept for plist compat; wait_for_wake_signal no longer waits.
# Keep child stdout/stderr on a durable file. SSH callers can disconnect while
# Python is finishing; flushing a closed pipe changes a successful exit to 120.
# A log-open failure remains a real failure; never run with unsafe stdio.
STDIO_LOG="$DATA_DIR/logs/stderr.log"
if ! touch "$STDIO_LOG" 2>/dev/null; then
  guard_notify "日报运行失败：无法打开标准错误日志 $STDIO_LOG"
  guard_heartbeat daily 1
  exit 1
fi
RUN_STARTED_AT="$(date +%s 2>/dev/null || echo 0)"
case "$RUN_STARTED_AT" in
  ''|*[!0-9]*) RUN_STARTED_AT=0 ;;
esac
/usr/bin/caffeinate -is "$PY" "$PROJECT/run_daily.py" --date "$REPORT_DATE" --skip-if-done --wait-for-wake \
  ${CHAT_DAILY_WAKE_DEADLINE:+--wake-deadline "$CHAT_DAILY_WAKE_DEADLINE"} >> "$STDIO_LOG" 2>&1
rc=$?

# Python can exit 120 while flushing a closed stdout/stderr stream after the
# application already completed. Treat that narrow case as success only when
# this run produced a fresh delivery marker; every other non-zero exit remains
# a failure and still alerts.
REPORT_ARCHIVE_DIR="$DATA_DIR/archive/${REPORT_DATE:0:4}/${REPORT_DATE:5:2}/${REPORT_DATE:8:2}"
COMPLETION_MARKER="$REPORT_ARCHIVE_DIR/.run-complete"
if [ "$rc" -eq 120 ] && [ -f "$COMPLETION_MARKER" ]; then
  MARKER_MTIME="$(stat -f %m "$COMPLETION_MARKER" 2>/dev/null || stat -c %Y "$COMPLETION_MARKER" 2>/dev/null || echo 0)"
  case "$MARKER_MTIME" in
    ''|*[!0-9]*) MARKER_MTIME=0 ;;
esac
  if [ "$RUN_STARTED_AT" -gt 0 ] && [ "$MARKER_MTIME" -ge "$RUN_STARTED_AT" ]; then
    echo "$(date '+%F %T') normalized child exit=120: fresh .run-complete exists" >> "$LOG"
    rc=0
  fi
fi

if [ "$rc" -ne 0 ]; then
  guard_notify "日报运行失败 exit=${rc}，详见 $REPORT_LOG"
fi
guard_heartbeat daily "$rc"
exit "$rc"
