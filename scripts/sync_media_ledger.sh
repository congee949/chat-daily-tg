#!/usr/bin/env bash
# sync_media_ledger.sh — Mac pulls r4s media_sent_ledger.jsonl for Podcast 👍 lookup.
#
# B站 / YouTube 订阅卡在 r4s 写出 write-after-send ledger；Podcast4bot 在 Mac 上
# 读 ~/chat-daily/state/media_sent_ledger.jsonl。本脚本用 rsync（scp 回退）
# 把远端文件拉到本地，由 launchd 每分钟跑一次。
#
# 用法：
#   ./scripts/sync_media_ledger.sh          # 拉取并覆盖本地
#   ./scripts/sync_media_ledger.sh --check  # 只报告远端/本地行数，不写盘
#
# 环境覆盖：
#   LEDGER_SYNC_HOST     默认 r4s
#   LEDGER_SYNC_REMOTE   默认 /root/chat-daily/state/media_sent_ledger.jsonl
#   LEDGER_SYNC_LOCAL    默认 ~/chat-daily/state/media_sent_ledger.jsonl
#   LEDGER_SYNC_LOG_DIR  默认 ~/chat-daily/logs
set -euo pipefail

HOST="${LEDGER_SYNC_HOST:-r4s}"
REMOTE="${LEDGER_SYNC_REMOTE:-/root/chat-daily/state/media_sent_ledger.jsonl}"
LOCAL="${LEDGER_SYNC_LOCAL:-$HOME/chat-daily/state/media_sent_ledger.jsonl}"
LOG_DIR="${LEDGER_SYNC_LOG_DIR:-$HOME/chat-daily/logs}"
LOG="$LOG_DIR/ledger-sync-$(date +%F).log"

CHECK_ONLY=0
[ "${1:-}" = "--check" ] && CHECK_ONLY=1

mkdir -p "$LOG_DIR" "$(dirname "$LOCAL")"

log() {
  local msg
  msg="$(date '+%F %T') $*"
  echo "$msg" | tee -a "$LOG"
}

SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new)

# Remote missing → skip (not an error): digest may not have written yet.
if ! ssh "${SSH_OPTS[@]}" "$HOST" "test -f '$REMOTE'" 2>/dev/null; then
  log "skip: remote ledger missing (${HOST}:${REMOTE})"
  exit 0
fi

remote_lines="$(ssh "${SSH_OPTS[@]}" "$HOST" "wc -l < '$REMOTE'" 2>/dev/null | tr -d '[:space:]' || echo "?")"
local_lines="0"
if [ -f "$LOCAL" ]; then
  local_lines="$(wc -l < "$LOCAL" | tr -d '[:space:]')"
fi

if [ "$CHECK_ONLY" = "1" ]; then
  log "check: remote=${remote_lines} local=${local_lines} (${HOST}:${REMOTE} → ${LOCAL})"
  exit 0
fi

tmp="$(mktemp "${TMPDIR:-/tmp}/media_sent_ledger.XXXXXX")"
cleanup() { rm -f "$tmp"; }
trap cleanup EXIT

pulled=0
if command -v rsync >/dev/null 2>&1; then
  if rsync -az -e "ssh ${SSH_OPTS[*]}" "${HOST}:${REMOTE}" "$tmp" 2>>"$LOG"; then
    pulled=1
  else
    log "warn: rsync failed, falling back to scp"
  fi
fi

if [ "$pulled" -eq 0 ]; then
  if ! scp "${SSH_OPTS[@]}" "${HOST}:${REMOTE}" "$tmp" 2>>"$LOG"; then
    log "error: scp failed (${HOST}:${REMOTE})"
    exit 1
  fi
  pulled=1
fi

# Atomic replace so readers never see a partial file.
mv -f "$tmp" "$LOCAL"
trap - EXIT

new_lines="$(wc -l < "$LOCAL" | tr -d '[:space:]')"
log "ok: synced ${remote_lines}→${new_lines} lines (was local=${local_lines}) → ${LOCAL}"
exit 0
