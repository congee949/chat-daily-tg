# guard_common.sh — shared launchd-guard helpers. SOURCED by run_*_guarded.sh,
# never executed directly. Callers must set: PROJECT, DATA_DIR, PY, PROXY, LOG,
# GUARD_TITLE.

# Route the Python pipeline's outbound traffic through the same local http proxy
# the alert path uses, and flag in-Python Telegram alerts on. Without the proxy
# export the httpx clients go DIRECT and Telegram pushes time out whenever
# Shadowrocket's global TUN isn't active (review finding #14). NO_PROXY keeps the
# local qwenproxy (embeddings/vision on :3000) direct.
guard_setup_env() {
  export HTTPS_PROXY="$PROXY" HTTP_PROXY="$PROXY"
  export NO_PROXY="127.0.0.1,localhost,::1"
  export no_proxy="$NO_PROXY"
  # Let notify_failure send Telegram alerts (it stays silent without this, so it
  # never fires in tests/ad-hoc runs).
  export CHAT_DAILY_TG_ALERTS=1
  export CHAT_DAILY_ALERT_PROXY="$PROXY"
}

# Alert: offline macOS notification first (never depends on network), then a
# best-effort Telegram message over the proxy (TG is unreachable direct here).
guard_notify() {
  local msg="$1"
  osascript -e "display notification \"${msg//\"/ }\" with title \"${GUARD_TITLE}\"" 2>/dev/null || true
  if [ -f "$DATA_DIR/.env" ]; then
    local tok cid thread tgt
    tok=$(grep -m1 '^TG_BOT_TOKEN=' "$DATA_DIR/.env" | cut -d= -f2-)
    cid=$(grep -m1 '^TG_CHAT_ID=' "$DATA_DIR/.env" | cut -d= -f2-)
    # Route to the 警告/alert forum topic when configured; fall back to DM cid.
    tgt=$(/usr/bin/python3 - "$cid" <<'PY' 2>/dev/null
import json, os, sys
dm = sys.argv[1]
try:
    t = json.load(open(os.path.expanduser("~/qwenproxy/.tg-notify-targets.json")))
    cid = t.get("chat_id", dm) or dm
    tid = (t.get("topics", {}) or {}).get("alert") or ""
except Exception:
    cid, tid = dm, ""
print(cid)
print(tid)
PY
)
    if [ -n "$tgt" ]; then
      { IFS= read -r cid; IFS= read -r thread; } <<< "$tgt"
    else
      thread=""
    fi
    if [ -n "$tok" ] && [ -n "$cid" ]; then
      curl -s --max-time 15 --proxy "$PROXY" \
        "https://api.telegram.org/bot${tok}/sendMessage" \
        --data-urlencode "chat_id=${cid}" \
        ${thread:+--data-urlencode "message_thread_id=${thread}"} \
        --data-urlencode "text=⚠️ ${GUARD_TITLE}: ${msg}" >/dev/null 2>&1 || true
    fi
  fi
  echo "$(date '+%F %T') ALERT: $msg" >> "$LOG"
}
