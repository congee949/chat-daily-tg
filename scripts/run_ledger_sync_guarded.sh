#!/bin/bash
# run_ledger_sync_guarded.sh — launchd wrapper for media_sent_ledger pull (r4s → Mac).
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
  rc=$?
  echo "$(date '+%F %T') end ledger-sync exit=$rc"
  exit "$rc"
} >>"$LOG" 2>&1
