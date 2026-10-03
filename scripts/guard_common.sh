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
  # kabi-tg-cli/Telethon does not read HTTP(S)_PROXY.  Give its dedicated
  # proxy setting the same known-good local endpoint so MTProto fetches do not
  # silently depend on Shadowrocket's current TUN mode.  Preserve an explicit
  # operator override for environments that need a different Telegram route.
  export TG_CLI_PROXY="${TG_CLI_PROXY:-$PROXY}"
}

# Alert: offline macOS notification first (never depends on network), then a
# best-effort Telegram message over the proxy (TG is unreachable direct here).
#
# Token must NOT appear on curl argv (ps/audit visibility). python3 reads
# $DATA_DIR/.env itself; only non-secret paths/title/msg ride argv.
guard_notify() {
  local msg="$1"
  osascript -e "display notification \"${msg//\"/ }\" with title \"${GUARD_TITLE}\"" 2>/dev/null || true
  if [ -f "$DATA_DIR/.env" ]; then
    # Args: env_path, proxy, title, msg, targets_json (optional path).
    # Bot token is loaded inside Python from env_path — never interpolated here.
    /usr/bin/python3 - "$DATA_DIR/.env" "${PROXY:-}" "$GUARD_TITLE" "$msg" \
      "${HOME}/qwenproxy/.tg-notify-targets.json" <<'PY' >/dev/null 2>&1 || true
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

env_path, proxy, title, msg, targets_path = sys.argv[1:6]

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
cid = env.get("TG_CHAT_ID") or ""
thread = ""

try:
    with open(os.path.expanduser(targets_path), "r", encoding="utf-8") as fh:
        t = json.load(fh)
    cid = str(t.get("chat_id") or cid or "")
    thread = str((t.get("topics") or {}).get("alert") or "")
except Exception:
    pass

if not tok or not cid:
    sys.exit(0)

# Build URL inside Python so the token never lands on a shell/curl argv.
url = "https://api.telegram.org/bot{}/sendMessage".format(tok)
form = {
    "chat_id": cid,
    "text": "⚠️ {}: {}".format(title, msg),
}
if thread:
    form["message_thread_id"] = thread
data = urllib.parse.urlencode(form).encode("utf-8")

handlers = []
if proxy:
    handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
else:
    handlers.append(urllib.request.ProxyHandler({}))
opener = urllib.request.build_opener(*handlers)
req = urllib.request.Request(url, data=data, method="POST")
try:
    with opener.open(req, timeout=15) as resp:
        resp.read()
except Exception:
    sys.exit(0)
PY
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
