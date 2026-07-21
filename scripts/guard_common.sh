# guard_common.sh — shared launchd-guard helpers. SOURCED by run_*_guarded.sh,
# never executed directly. Callers must set: PROJECT, DATA_DIR, PY, PROXY, LOG,
# GUARD_TITLE.

# Route the Python pipeline's outbound traffic through the same local http proxy
# the alert path uses, and flag in-Python Telegram alerts on. Without the proxy
# export the httpx clients go DIRECT and Telegram pushes time out whenever
# Shadowrocket's global TUN isn't active (review finding #14). NO_PROXY keeps the
# local model proxy (CLIProxyAPI on :8317 — summary/vision/judge) direct.
guard_setup_env() {
  export HTTPS_PROXY="$PROXY" HTTP_PROXY="$PROXY"
  export NO_PROXY="127.0.0.1,localhost,::1"
  export no_proxy="$NO_PROXY"
  # Drop any inherited ALL_PROXY (Shadowrocket / `launchctl setenv` often leaves a
  # socks5:// value here). httpx.Client() eagerly builds a transport for EVERY proxy
  # env var at construction, so a stray socks5 ALL_PROXY makes every client raise
  # ImportError("'socksio' not installed") before a single request — the crash that
  # took out the 2026-07-03 run even though HTTP(S)_PROXY above point at the http proxy.
  unset ALL_PROXY all_proxy
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

# Heartbeat to task-monitor center (spec: fail-open, never affects the task).
# Usage: guard_heartbeat <name> <rc>. --noproxy so the tailscale POST isn't
# hijacked by the HTTPS_PROXY guard_setup_env exports.
guard_heartbeat() {
  local st err=""
  [ "$2" -eq 0 ] && st=ok || st=fail
  [ "$2" -ne 0 ] && [ -f "$LOG" ] && err=$(tail -c 200 "$LOG" 2>/dev/null)
  curl -s --max-time 8 --connect-timeout 2 --noproxy '*' -X POST \
    "${HB_CENTER:-http://100.87.113.14:8900}/hb/$1?status=${st}&exit=$2" \
    --data-urlencode "error=${err}" >/dev/null 2>&1 || true
}

# Random delay after a calendar launchd fire so pushes are not wall-clock aligned.
# Opt-in per wrapper (agent must NOT call this). Env:
#   CHAT_DAILY_NO_JITTER=1          — skip entirely (manual catch-up / tests)
#   CHAT_DAILY_JITTER_MIN_S         — inclusive lower bound (default 0)
#   CHAT_DAILY_JITTER_MAX_S         — inclusive upper bound (default 900 = 15min)
# Uses awk srand for inclusive [min,max] (same approach as due_gate on r4s).
# Requires caller to have set LOG (append-only); no-op logging if LOG unset.
guard_jitter_sleep() {
  if [ "${CHAT_DAILY_NO_JITTER:-}" = "1" ]; then
    echo "$(date '+%F %T') jitter skipped (CHAT_DAILY_NO_JITTER=1)" >> "${LOG:-/dev/null}" 2>/dev/null || true
    return 0
  fi

  local min_s max_s delay
  min_s="${CHAT_DAILY_JITTER_MIN_S:-0}"
  max_s="${CHAT_DAILY_JITTER_MAX_S:-900}"

  # Non-negative integers only; fall back to defaults on garbage.
  case "$min_s" in ''|*[!0-9]*) min_s=0 ;; esac
  case "$max_s" in ''|*[!0-9]*) max_s=900 ;; esac
  if [ "$min_s" -gt "$max_s" ]; then
    echo "$(date '+%F %T') jitter invalid range min=$min_s max=$max_s; skipping" \
      >> "${LOG:-/dev/null}" 2>/dev/null || true
    return 0
  fi

  delay=$(awk -v min="$min_s" -v max="$max_s" 'BEGIN {
    srand()
    print int(min + rand() * (max - min + 1))
  }')
  case "$delay" in ''|*[!0-9]*) delay=0 ;; esac

  echo "$(date '+%F %T') jitter delay=${delay}s range=[${min_s},${max_s}]" \
    >> "${LOG:-/dev/null}" 2>/dev/null || true

  if [ "$delay" -gt 0 ]; then
    sleep "$delay"
  fi
}
