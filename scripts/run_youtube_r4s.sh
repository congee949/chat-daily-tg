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
#   CPA calls use its Tailscale address directly via NO_PROXY.
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
export NO_PROXY="127.0.0.1,localhost,::1,100.87.113.14" no_proxy="127.0.0.1,localhost,::1,100.87.113.14"
export CHAT_DAILY_TG_ALERTS=1 CHAT_DAILY_ALERT_PROXY="$PROXY"
export PYTHONPATH="$PROJECT/src"
export CHAT_DAILY_DATA_DIR="$DATA_DIR"
export CHAT_DAILY_HERMES_INCIDENT_ENDPOINT="http://127.0.0.1:28768"
export CHAT_DAILY_HERMES_INCIDENT_TOKEN_FILE="/opt/hermes/data/incident-controller/ingest.token"

# Dedup window for shell-side alerts (seconds). run_daily also calls
# notify_failure on digest exceptions — during a multi-tick RSS storm the
# */5 due_gate reopen used to twin-spam the alert topic (python + shell)
# every few minutes. Throttle the shell path; python path stays for the
# first failure's exception detail.
ALERT_THROTTLE_S=1200
ALERT_STAMP="$DATA_DIR/state/youtube-alert-last"
ALERT_STAMP_FALLBACK="/tmp/chat-daily-alert-throttle-youtube-guard"

_alert_stamp_last() {
  last=0
  for stamp in "$ALERT_STAMP" "$ALERT_STAMP_FALLBACK"; do
    [ -f "$stamp" ] || continue
    val=$(cat "$stamp" 2>/dev/null || echo 0)
    if [ -n "$val" ] && [ "$val" -eq "$val" ] 2>/dev/null; then
      if [ "$val" -gt "$last" ]; then
        last=$val
      fi
    fi
  done
  echo "$last"
}

_schedule_due() {
  if [ -x "$DUE_GATE" ] || [ -f "$DUE_GATE" ]; then
    /bin/sh "$DUE_GATE" schedule youtube "$DUE_MIN_S" "$DUE_MAX_S" || \
      echo "$(date '+%F %T') WARN: due_gate schedule youtube failed" >> "$LOG"
  fi
}

alert() {
  # Best-effort TG alert to the alert topic. Token is read inside python from
  # .env so it never appears on curl argv (ps visibility on a shared r4s).
  echo "$(date '+%F %T') ALERT: $1" >> "$LOG" 2>/dev/null || true
  now=$(date +%s)
  last=$(_alert_stamp_last)
  if [ "$last" -gt 0 ] 2>/dev/null; then
    delta=$((now - last))
    if [ "$delta" -ge 0 ] && [ "$delta" -lt "$ALERT_THROTTLE_S" ]; then
      echo "$(date '+%F %T') alert throttled (${delta}s < ${ALERT_THROTTLE_S}s): $1" >> "$LOG" 2>/dev/null || true
      return 0
    fi
  fi
  # Stamp only after a successful send attempt returns 0 from python.
  if python3 - "$DATA_DIR/.env" "$PROXY" "$1" <<'PY' >/dev/null 2>&1
import json
import sys
import urllib.parse
import urllib.request

env_path, proxy, msg = sys.argv[1:4]

def _load_env(path):
    out = {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out

env = _load_env(env_path)
tok = env.get("TG_BOT_TOKEN") or ""
chat = ""
thread = ""
try:
    with open("/root/qwenproxy/.tg-notify-targets.json", "r", encoding="utf-8") as fh:
        t = json.load(fh)
    chat = str(t.get("chat_id") or "")
    thread = str((t.get("topics") or {}).get("alert") or "")
except Exception:
    pass
if not tok or not chat:
    raise SystemExit(1)
url = "https://api.telegram.org/bot{}/sendMessage".format(tok)
form = {
    "chat_id": chat,
    "text": "⚠️ chat-daily-tg YouTube守护(r4s): {}".format(msg),
}
if thread:
    form["message_thread_id"] = thread
data = urllib.parse.urlencode(form).encode("utf-8")
handlers = []
if proxy:
    handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
opener = urllib.request.build_opener(*handlers)
req = urllib.request.Request(url, data=data, method="POST")
try:
    with opener.open(req, timeout=15) as resp:
        resp.read()
except Exception:
    raise SystemExit(1)
raise SystemExit(0)
PY
  then
    mkdir -p "$DATA_DIR/state" 2>/dev/null || true
    if ! echo "$now" > "$ALERT_STAMP" 2>/dev/null; then
      echo "$now" > "$ALERT_STAMP_FALLBACK" 2>/dev/null || true
    fi
  fi
}

exec 9>"$LOCK"
if ! flock -n 9; then
  echo "$(date '+%F %T') skipped: previous round still running" >> "$LOG"
  exit 0
fi

cd "$PROJECT" || { alert "项目目录缺失 $PROJECT"; exit 1; }
python3 run_daily.py --youtube-only
rc=$?
if [ "$rc" -eq 2 ]; then
  alert "YouTube digest 磁盘满 exit=2，已降频；详见 $DATA_DIR/logs/youtube-$(date +%F).log"
  _schedule_due
  exit 2
fi
if [ "$rc" -ne 0 ]; then
  alert "YouTube digest 失败 exit=$rc，详见 $DATA_DIR/logs/youtube-$(date +%F).log"
  # Leave due gate open so next */5 retries; do not schedule.
  exit "$rc"
fi

# Success only: roll next random interval (10–15 min).
_schedule_due
exit 0
