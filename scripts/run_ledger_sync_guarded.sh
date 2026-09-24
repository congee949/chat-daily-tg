#!/bin/bash
# run_ledger_sync_guarded.sh — launchd wrapper for media pull and sent-content push.
#
# Thin guard: only timestamps + exit code into the daily guard log. No venv /
# caffeinate / TG alert — this is a short rsync; failures are transient SSH
# blips more often than real outages, and StartInterval=60 would spam if we
# alerted every miss. Inspect guard-ledger-sync-*.log / ledger-sync-*.log.
#
# Overridable: CHAT_DAILY_DATA_DIR, LEDGER_SYNC_* (passed through to sync script).
set -uo pipefail

PROJECT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA_DIR="${CHAT_DAILY_DATA_DIR:-$HOME/chat-daily}"
LOG="$DATA_DIR/logs/guard-ledger-sync-$(date +%F).log"
mkdir -p "$DATA_DIR/logs"

{
  echo "$(date '+%F %T') start ledger-sync"
  "$PROJECT/scripts/sync_media_ledger.sh"
  media_rc=$?
  "$PROJECT/scripts/sync_sent_content_ledger.sh"
  content_rc=$?
  rc=$media_rc
  if [ "$rc" -eq 0 ] && [ "$content_rc" -ne 0 ]; then
    rc=$content_rc
  fi
  echo "$(date '+%F %T') end ledger-sync exit=$rc"
  exit "$rc"
} >>"$LOG" 2>&1
