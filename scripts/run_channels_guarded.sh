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
# CHAT_DAILY_NO_JITTER, CHAT_DAILY_JITTER_MIN_S, CHAT_DAILY_JITTER_MAX_S,
# CHAT_DAILY_CHANNELS_LOCK_DIR.
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

# launchd suppresses overlap for one label, but a manual catch-up can still race
# the scheduled job. SeenStore is intentionally write-after-send, so two forwarders
# that start from the same snapshot can both send before either appends its key.
#
# Lock protocol (empty/dead-pid TOCTOU-safe):
#   1) mkdir LOCK_DIR — primary acquire; write pid immediately after
#   2) live owner (or young lock with missing pid) → "skip locked"
#   3) stale reclaim under exclusive LOCK_DIR.reclaim mutex; only one reclaimer
#      removes the dead dir and re-creates. Concurrent N → one holder.
LOCK_DIR="${CHAT_DAILY_CHANNELS_LOCK_DIR:-/tmp/chat-daily-tg-channels.lock}"
RECLAIM_DIR="${LOCK_DIR}.reclaim"

_channels_lock_release() {
  # Drop only if we still own the pid file (avoid clobbering a successor).
  if [ -f "$LOCK_DIR/pid" ]; then
    _owner="$(cat "$LOCK_DIR/pid" 2>/dev/null || true)"
    if [ "$_owner" = "$$" ]; then
      rm -f "$LOCK_DIR/pid"
      rmdir "$LOCK_DIR" 2>/dev/null || true
    fi
  fi
}

# Returns 0 when LOCK_DIR is held by a live (or still-pending) owner.
_channels_lock_is_live() {
  local old_pid now mtime age
  if [ ! -d "$LOCK_DIR" ]; then
    return 1
  fi
  old_pid="$(cat "$LOCK_DIR/pid" 2>/dev/null || true)"
  case "$old_pid" in
    ''|*[!0-9]*)
      # Missing/empty/garbage pid: treat as held during a short grace window so a
      # peer mid-write is not reclaimed; after grace, consider it stale.
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

_channels_lock_acquire() {
  # Primary acquire: exclusive directory create.
  if mkdir "$LOCK_DIR" 2>/dev/null; then
    echo $$ > "$LOCK_DIR/pid"
    return 0
  fi

  if _channels_lock_is_live; then
    old_pid="$(cat "$LOCK_DIR/pid" 2>/dev/null || true)"
    echo "$(date '+%F %T') skip locked: channels already running pid=${old_pid:-pending}" >> "$LOG"
    return 1
  fi

  # Stale reclaim: only one process wins the reclaim mutex.
  if mkdir "$RECLAIM_DIR" 2>/dev/null; then
    # Re-check under leadership — a peer may have finished acquiring.
    if _channels_lock_is_live; then
      rmdir "$RECLAIM_DIR" 2>/dev/null || true
      old_pid="$(cat "$LOCK_DIR/pid" 2>/dev/null || true)"
      echo "$(date '+%F %T') skip locked: channels already running pid=${old_pid:-pending}" >> "$LOG"
      return 1
    fi
    rm -f "$LOCK_DIR/pid" 2>/dev/null || true
    if rmdir "$LOCK_DIR" 2>/dev/null; then
      if mkdir "$LOCK_DIR" 2>/dev/null; then
        echo $$ > "$LOCK_DIR/pid"
        rmdir "$RECLAIM_DIR" 2>/dev/null || true
        return 0
      fi
      # Brief gap after rmdir: a primary acquirer may have won; they hold the lock.
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

if ! _channels_lock_acquire; then
  # Heartbeat stays 0 (soft skip); log already said "skip locked".
  guard_heartbeat channels 0
  exit 0
fi
trap '_channels_lock_release' EXIT INT TERM

# Refresh the optional caption mirror only when this process owns the channel
# lock, before any Python channel work. A failed refresh keeps last-good state
# and must never prevent channel delivery.
"$PY" "$PROJECT/scripts/sync_xmonitor_sent_content.py" \
  --destination "$DATA_DIR/state/xmonitor_sent_snapshot.json" >>"$LOG" 2>&1
xmonitor_rc=$?
echo "$(date '+%F %T') xmonitor-caption-mirror exit=$xmonitor_rc" >>"$LOG"

# De-align from wall clock (0–15 min default); then caffeinate only the Python work.
# Parent sleep under launchd is fine while AC + disablesleep holds the machine awake.
guard_jitter_sleep
/usr/bin/caffeinate -is "$PY" "$PROJECT/run_daily.py" --channels-only
rc=$?
if [ "$rc" -ne 0 ]; then
  guard_notify "频道转发失败 exit=${rc}，详见 $DATA_DIR/logs/channels-$(date +%F).log"
fi
guard_heartbeat channels "$rc"
exit "$rc"
