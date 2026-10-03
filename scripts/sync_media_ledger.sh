#!/usr/bin/env bash
# sync_media_ledger.sh — Mac pulls r4s media_sent_ledger.jsonl for explicit historical-index refresh.
#
# B站 / YouTube 订阅卡在 r4s 写出 write-after-send ledger；Mac 历史知识索引可
# 读取 ~/chat-daily/state/media_sent_ledger.jsonl。本脚本优先 scp（r4s 无 rsync），
# 失败再 rsync 回退；拉到临时文件后做 JSONL 校验与缩量保护，再原子覆盖本地。
# 仅显式运行；ledger-sync 不再自动调用。
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
#
# 测试使用隔离路径和 SSH 替身，不覆盖生产账本。
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

# Count non-empty lines (macOS wc -l pads spaces).
_count_lines() {
  local f="$1"
  if [ ! -f "$f" ]; then
    echo 0
    return
  fi
  # Prefer non-empty line count so blank trailing lines do not inflate.
  awk 'NF {n++} END {print n+0}' "$f"
}

# Validate pulled JSONL: every non-empty line must json.loads and contain
# chat_id / message_id / url. Prints valid non-empty line count on success.
_validate_ledger_jsonl() {
  local f="$1"
  /usr/bin/python3 - "$f" <<'PY'
import json, sys
path = sys.argv[1]
n = 0
try:
    with open(path, "r", encoding="utf-8") as fh:
        for i, raw in enumerate(fh, 1):
            line = raw.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception as e:
                print(f"line {i}: json.loads failed: {e}", file=sys.stderr)
                sys.exit(2)
            if not isinstance(obj, dict):
                print(f"line {i}: expected object, got {type(obj).__name__}", file=sys.stderr)
                sys.exit(2)
            for key in ("chat_id", "message_id", "url"):
                if key not in obj:
                    print(f"line {i}: missing required key {key!r}", file=sys.stderr)
                    sys.exit(2)
            n += 1
except FileNotFoundError:
    print("file missing", file=sys.stderr)
    sys.exit(2)
if n == 0:
    print("empty jsonl (no non-empty lines)", file=sys.stderr)
    sys.exit(2)
print(n)
sys.exit(0)
PY
}

# Remote missing → skip (not an error): digest may not have written yet.
if ! ssh "${SSH_OPTS[@]}" "$HOST" "test -f '$REMOTE'" 2>/dev/null; then
  log "skip: remote ledger missing (${HOST}:${REMOTE})"
  exit 0
fi

remote_lines="$(ssh "${SSH_OPTS[@]}" "$HOST" "wc -l < '$REMOTE'" 2>/dev/null | tr -d '[:space:]' || echo "?")"
local_lines="0"
if [ -f "$LOCAL" ]; then
  local_lines="$(_count_lines "$LOCAL")"
fi

if [ "$CHECK_ONLY" = "1" ]; then
  log "check: remote=${remote_lines} local=${local_lines} (${HOST}:${REMOTE} → ${LOCAL})"
  exit 0
fi

tmp="$(mktemp "${TMPDIR:-/tmp}/media_sent_ledger.XXXXXX")"
cleanup() { rm -f "$tmp"; }
trap cleanup EXIT

# Prefer scp: r4s (OpenWrt/BusyBox) typically has no rsync; avoid noisy rsync attempts.
# Fall back to rsync only if scp fails (e.g. local dev host that only exposes rsync).
pulled=0
if scp "${SSH_OPTS[@]}" "${HOST}:${REMOTE}" "$tmp" 2>>"$LOG"; then
  pulled=1
else
  log "warn: scp failed, falling back to rsync"
  if command -v rsync >/dev/null 2>&1; then
    if rsync -az -e "ssh ${SSH_OPTS[*]}" "${HOST}:${REMOTE}" "$tmp" 2>>"$LOG"; then
      pulled=1
    fi
  fi
fi

if [ "$pulled" -eq 0 ]; then
  log "error: pull failed (${HOST}:${REMOTE})"
  exit 1
fi

# Validate before touching the last-good local file.
if ! valid_lines="$(_validate_ledger_jsonl "$tmp" 2>>"$LOG")"; then
  log "error: pulled ledger failed JSONL validation; keeping last-good at ${LOCAL}"
  exit 1
fi
new_lines="$valid_lines"

# Shrink guard: if local is substantial and remote collapsed, refuse overwrite.
# Threshold: local > 10 non-empty lines AND new < 80% of local → keep last-good.
if [ "$local_lines" -gt 10 ] 2>/dev/null; then
  # integer math: refuse when new * 100 < local * 80
  if [ "$((new_lines * 100))" -lt "$((local_lines * 80))" ]; then
    log "error: refuse overwrite: new=${new_lines} < 80% of local=${local_lines}; keeping last-good at ${LOCAL}"
    exit 1
  fi
fi

# Optional last-good backup, then atomic replace so readers never see a partial file.
if [ -f "$LOCAL" ]; then
  cp -f "$LOCAL" "${LOCAL}.bak" 2>>"$LOG" || log "warn: could not write ${LOCAL}.bak"
fi
mv -f "$tmp" "$LOCAL"
trap - EXIT

log "ok: synced ${remote_lines}→${new_lines} lines (was local=${local_lines}) → ${LOCAL}"
exit 0
