#!/usr/bin/env bash
# Push the Mac-owned chatdaily sent-content ledger to the R4S Hermes reader.
#
# This is intentionally separate from media_sent_ledger.jsonl: the media ledger
# is authored on R4S and pulled to the Mac, while this general-content ledger is
# authored on the Mac and pushed to an independent R4S read-only replica.
set -euo pipefail

HOST="${SENT_CONTENT_SYNC_HOST:-r4s}"
LOCAL="${SENT_CONTENT_SYNC_LOCAL:-$HOME/chat-daily/state/sent_content_ledger.jsonl}"
REMOTE="${SENT_CONTENT_SYNC_REMOTE:-/root/chat-daily/state/chatdaily_sent_content_ledger.jsonl}"
LOG_DIR="${SENT_CONTENT_SYNC_LOG_DIR:-$HOME/chat-daily/logs}"
LOG="$LOG_DIR/sent-content-sync-$(date +%F).log"
CHECK_ONLY=0
[ "${1:-}" = "--check" ] && CHECK_ONLY=1

mkdir -p "$LOG_DIR"

log() {
  echo "$(date '+%F %T') $*" | tee -a "$LOG"
}

SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new)

validate_local() {
  /usr/bin/python3 - "$1" <<'PY'
import hashlib
import json
import sys

path = sys.argv[1]
count = 0
with open(path, "r", encoding="utf-8") as handle:
    for line_number, raw in enumerate(handle, 1):
        line = raw.strip()
        if not line:
            continue
        row = json.loads(line)
        if not isinstance(row, dict) or row.get("schema") != "sent-content.v1":
            raise SystemExit(f"line {line_number}: invalid schema")
        for key in (
            "chat_id", "message_id", "producer", "source_kind", "source_ref",
            "url", "content", "content_hash", "delivery_state", "sent_at",
        ):
            if key not in row:
                raise SystemExit(f"line {line_number}: missing {key}")
        if row["delivery_state"] != "confirmed":
            raise SystemExit(f"line {line_number}: non-confirmed delivery")
        actual = hashlib.sha256(str(row["content"]).encode("utf-8")).hexdigest()
        if actual != row["content_hash"]:
            raise SystemExit(f"line {line_number}: content hash mismatch")
        count += 1
if count < 1:
    raise SystemExit("ledger has no records")
print(count)
PY
}

if [ ! -f "$LOCAL" ]; then
  log "skip: local sent-content ledger missing (${LOCAL})"
  exit 0
fi

if ! local_lines="$(validate_local "$LOCAL" 2>>"$LOG")"; then
  log "error: local sent-content ledger failed validation; remote last-good unchanged"
  exit 1
fi

remote_lines="$(
  ssh "${SSH_OPTS[@]}" "$HOST" \
    "if [ -f '$REMOTE' ]; then awk 'NF {n++} END {print n+0}' '$REMOTE'; else echo 0; fi" \
    2>/dev/null || echo "?"
)"

if [ "$CHECK_ONLY" = "1" ]; then
  log "check: local=${local_lines} remote=${remote_lines} (${LOCAL} → ${HOST}:${REMOTE})"
  exit 0
fi

if [[ "$remote_lines" =~ ^[0-9]+$ ]] && [ "$remote_lines" -gt 10 ]; then
  if [ "$((local_lines * 100))" -lt "$((remote_lines * 80))" ]; then
    log "error: refuse shrink: local=${local_lines} < 80% of remote=${remote_lines}"
    exit 1
  fi
fi

remote_tmp="${REMOTE}.tmp.$$"
cleanup_remote() {
  ssh "${SSH_OPTS[@]}" "$HOST" "rm -f '$remote_tmp'" >/dev/null 2>&1 || true
}
trap cleanup_remote EXIT

ssh "${SSH_OPTS[@]}" "$HOST" "mkdir -p '$(dirname "$REMOTE")'"
scp "${SSH_OPTS[@]}" "$LOCAL" "${HOST}:${remote_tmp}" 2>>"$LOG"

# Re-validate on the target and atomically replace the reader-visible file.
ssh "${SSH_OPTS[@]}" "$HOST" "/usr/bin/python3 - '$remote_tmp' '$REMOTE'" <<'PY'
import hashlib
import json
import os
import sys

source, destination = sys.argv[1:]
count = 0
with open(source, "r", encoding="utf-8") as handle:
    for line_number, raw in enumerate(handle, 1):
        line = raw.strip()
        if not line:
            continue
        row = json.loads(line)
        if not isinstance(row, dict) or row.get("schema") != "sent-content.v1":
            raise SystemExit(f"line {line_number}: invalid schema")
        if row.get("delivery_state") != "confirmed":
            raise SystemExit(f"line {line_number}: non-confirmed delivery")
        actual = hashlib.sha256(str(row.get("content", "")).encode("utf-8")).hexdigest()
        if actual != row.get("content_hash"):
            raise SystemExit(f"line {line_number}: content hash mismatch")
        count += 1
if count < 1:
    raise SystemExit("ledger has no records")
os.chmod(source, 0o600)
os.replace(source, destination)
print(count)
PY
trap - EXIT

log "ok: synced ${local_lines} lines (was remote=${remote_lines}) → ${HOST}:${REMOTE}"
