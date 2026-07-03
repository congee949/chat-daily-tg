#!/bin/sh
# run_bilibili_r4s.sh — r4s (FriendlyWrt) cron wrapper for the hourly Bilibili
# digest, the r4s counterpart of run_bilibili_guarded.sh (launchd/macOS).
#
# Deploy: git archive → /root/chat-daily-tg (this script rides along).
# Cron:   30 * * * * /bin/sh /root/chat-daily-tg/scripts/run_bilibili_r4s.sh
#
# Environment notes (musl/OpenWrt specifics):
# - TZ=CST-8: POSIX form — named zones (Asia/Shanghai) silently fall back to
#   UTC without an IANA db, which would skew card publish times by 8h.
# - Egress: TG/Gemini ride the bwg tinyproxy over tailscale
#   (100.87.113.14:8888). Bilibili calls ignore it (trust_env=False, direct
#   China exit — the whole reason fetch runs on r4s and not bwg).
# - flock: cron has no launchd-style same-label suppression; a slow round must
#   not overlap the next one (duplicate cards via double SeenStore read).
set -u

PROJECT="/root/chat-daily-tg"
DATA_DIR="/root/chat-daily"
PROXY="http://100.87.113.14:8888"
LOCK="/tmp/chat-daily-bilibili.lock"
LOG="$DATA_DIR/logs/guard-bilibili-$(TZ=CST-8 date +%F).log"
mkdir -p "$DATA_DIR/logs"

export TZ=CST-8
export HTTPS_PROXY="$PROXY" HTTP_PROXY="$PROXY"
export NO_PROXY="127.0.0.1,localhost,::1" no_proxy="127.0.0.1,localhost,::1"
export CHAT_DAILY_TG_ALERTS=1 CHAT_DAILY_ALERT_PROXY="$PROXY"
export PYTHONPATH="$PROJECT/src"

alert() {
  # Best-effort TG alert to the alert topic; mirrors guard_notify's TG branch.
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
    --data-urlencode "text=⚠️ chat-daily-tg B站守护(r4s): $1" >/dev/null 2>&1
  echo "$(date '+%F %T') ALERT: $1" >> "$LOG"
}

exec 9>"$LOCK"
if ! flock -n 9; then
  echo "$(date '+%F %T') skipped: previous round still running" >> "$LOG"
  exit 0
fi

cd "$PROJECT" || { alert "项目目录缺失 $PROJECT"; exit 1; }
python3 run_daily.py --bilibili-only
rc=$?
if [ "$rc" -ne 0 ]; then
  alert "B站 digest 失败 exit=$rc，详见 $DATA_DIR/logs/bilibili-$(date +%F).log"
fi
exit "$rc"
