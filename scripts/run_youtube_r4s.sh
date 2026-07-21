#!/bin/sh
# run_youtube_r4s.sh — r4s (FriendlyWrt) cron wrapper for the YouTube digest.
# Sole scheduler for the digest (never ran on Mac).
#
# Deploy: git archive → /root/chat-daily-tg (this script rides along).
#
# Cron (scheme B — random 10–15 min via due_gate + */5 probe):
#   */5 * * * * /bin/sh /root/chat-daily-tg/scripts/due_gate.sh check youtube \
#     && /root/bin/hb-wrap youtube -- /bin/sh /root/chat-daily-tg/scripts/run_youtube_r4s.sh
# due_gate MUST sit before hb-wrap so not-due ticks don't fake-green heartbeats.
# Next interval is scheduled only after SUCCESS (failures leave gate open → retry).
#
# Environment notes (musl/OpenWrt specifics):
# - TZ=CST-8: POSIX form — named zones (Asia/Shanghai) silently fall back to
#   UTC without an IANA db, which would skew card publish times by 8h.
# - Egress: EVERYTHING here (YouTube RSS / googleapis / i.ytimg.com covers /
#   TG push) rides the bwg tinyproxy over tailscale (100.87.113.14:8888) —
#   the REVERSE of the Bilibili wrapper, where fetch must go direct. No
#   NO_PROXY carve-out is needed beyond localhost.
# - flock: cron has no launchd-style same-label suppression; a slow round must
#   not overlap the next one (duplicate cards via double SeenStore read).
set -u

PROJECT="/root/chat-daily-tg"
DATA_DIR="/root/chat-daily"
PROXY="http://100.87.113.14:8888"
LOCK="/tmp/chat-daily-youtube.lock"
LOG="$DATA_DIR/logs/guard-youtube-$(TZ=CST-8 date +%F).log"
DUE_MIN_S=600
DUE_MAX_S=900
DUE_GATE="$PROJECT/scripts/due_gate.sh"
mkdir -p "$DATA_DIR/logs"

export TZ=CST-8
export HTTPS_PROXY="$PROXY" HTTP_PROXY="$PROXY"
export NO_PROXY="127.0.0.1,localhost,::1" no_proxy="127.0.0.1,localhost,::1"
export CHAT_DAILY_TG_ALERTS=1 CHAT_DAILY_ALERT_PROXY="$PROXY"
export PYTHONPATH="$PROJECT/src"
export CHAT_DAILY_DATA_DIR="$DATA_DIR"

# Dedup window for shell-side alerts (seconds). run_daily also calls
# notify_failure on digest exceptions — during a multi-tick RSS storm the
# */5 due_gate reopen used to twin-spam the alert topic (python + shell)
# every few minutes. Throttle the shell path; python path stays for the
# first failure's exception detail.
ALERT_THROTTLE_S=1200
ALERT_STAMP="$DATA_DIR/state/youtube-alert-last"

alert() {
  # Best-effort TG alert to the alert topic; mirrors guard_notify's TG branch.
  echo "$(date '+%F %T') ALERT: $1" >> "$LOG"
  mkdir -p "$DATA_DIR/state"
  now=$(date +%s)
  if [ -f "$ALERT_STAMP" ]; then
    last=$(cat "$ALERT_STAMP" 2>/dev/null || echo 0)
    # BusyBox date/expr safe: skip TG if last alert was within throttle window.
    if [ -n "$last" ] && [ "$last" -eq "$last" ] 2>/dev/null; then
      delta=$((now - last))
      if [ "$delta" -ge 0 ] && [ "$delta" -lt "$ALERT_THROTTLE_S" ]; then
        echo "$(date '+%F %T') alert throttled (${delta}s < ${ALERT_THROTTLE_S}s): $1" >> "$LOG"
        return 0
      fi
    fi
  fi
  tok=$(grep -m1 '^TG_BOT_TOKEN=' "$DATA_DIR/.env" 2>/dev/null | cut -d= -f2-)
  cid=$(python3 -c "
import json
try:
    t = json.load(open('/root/qwenproxy/.tg-notify-targets.json'))
    print(t.get('chat_id', '')); print((t.get('topics') or {}).get('alert') or '')
except Exception:
    print(); print()
" 2>/dev/null)
  chat=$(echo "$cid" | sed -n 1p); thread=$(echo "$cid" | sed -n 2p)
  [ -n "$tok" ] && [ -n "$chat" ] && curl -s --max-time 15 --proxy "$PROXY" \
    "https://api.telegram.org/bot${tok}/sendMessage" \
    --data-urlencode "chat_id=${chat}" \
    ${thread:+--data-urlencode "message_thread_id=${thread}"} \
    --data-urlencode "text=⚠️ chat-daily-tg YouTube守护(r4s): $1" >/dev/null 2>&1 \
    && echo "$now" > "$ALERT_STAMP"
}

exec 9>"$LOCK"
if ! flock -n 9; then
  echo "$(date '+%F %T') skipped: previous round still running" >> "$LOG"
  exit 0
fi

cd "$PROJECT" || { alert "项目目录缺失 $PROJECT"; exit 1; }
python3 run_daily.py --youtube-only
rc=$?
if [ "$rc" -ne 0 ]; then
  alert "YouTube digest 失败 exit=$rc，详见 $DATA_DIR/logs/youtube-$(date +%F).log"
  # Leave due gate open so next */5 retries; do not schedule.
  exit "$rc"
fi

# Success only: roll next random interval (10–15 min).
if [ -x "$DUE_GATE" ] || [ -f "$DUE_GATE" ]; then
  /bin/sh "$DUE_GATE" schedule youtube "$DUE_MIN_S" "$DUE_MAX_S" || \
    echo "$(date '+%F %T') WARN: due_gate schedule youtube failed" >> "$LOG"
fi
exit 0
