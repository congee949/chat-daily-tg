#!/bin/sh
# due_gate.sh — random-interval gate for cron (scheme B).
#
# cron cannot natively schedule random intervals. Probe every */5 and only
# run the job when the due stamp has passed. Schedule the *next* interval
# only after a SUCCESSFUL run (wrappers call `schedule`); failures leave
# the gate open so the next */5 tick retries.
#
# Usage:
#   due_gate.sh check <name>                 # exit 0 due, 1 not due, 2 usage
#   due_gate.sh schedule <name> <min_s> <max_s>
#
# State file: $CHAT_DAILY_DATA_DIR/state/due-<name>.next  (epoch seconds)
# Default DATA_DIR=/root/chat-daily via CHAT_DAILY_DATA_DIR.
#
# Crontab examples (due_gate MUST run BEFORE hb-wrap so skip ticks don't
# fake-green heartbeats):
#   */5 * * * * /bin/sh /root/chat-daily-tg/scripts/due_gate.sh check bilibili \
#     && /root/bin/hb-wrap bilibili -- /bin/sh /root/chat-daily-tg/scripts/run_bilibili_r4s.sh
#   */5 * * * * /bin/sh /root/chat-daily-tg/scripts/due_gate.sh check youtube \
#     && /root/bin/hb-wrap youtube -- /bin/sh /root/chat-daily-tg/scripts/run_youtube_r4s.sh
#
# Intervals (set in wrappers on success):
#   bilibili: 1200–1800s (20–30 min)
#   youtube:    600–900s (10–15 min)
#
# POSIX sh / OpenWrt ash: no $RANDOM; random via awk srand.
set -u

DATA_DIR="${CHAT_DAILY_DATA_DIR:-/root/chat-daily}"
STATE_DIR="$DATA_DIR/state"
LOG_DIR="$DATA_DIR/logs"

usage() {
  echo "usage: due_gate.sh check <name>" >&2
  echo "       due_gate.sh schedule <name> <min_s> <max_s>" >&2
  exit 2
}

stamp_path() {
  # $1 = name
  echo "$STATE_DIR/due-$1.next"
}

# Return 0 if $1 is a pure non-negative integer (digits only).
is_uint() {
  case "${1:-}" in
    ''|*[!0-9]*) return 1 ;;
    *) return 0 ;;
  esac
}

cmd_check() {
  name="${1:-}"
  [ -n "$name" ] || usage
  stamp="$(stamp_path "$name")"

  # Missing stamp → due (first run / after wipe).
  if [ ! -f "$stamp" ]; then
    exit 0
  fi

  due=$(cat "$stamp" 2>/dev/null || true)
  # Corrupt / empty stamp → treat as due (self-heal on next success schedule).
  if ! is_uint "$due"; then
    exit 0
  fi

  now=$(date +%s)
  if [ "$now" -ge "$due" ]; then
    exit 0
  fi
  exit 1
}

cmd_schedule() {
  name="${1:-}"
  min_s="${2:-}"
  max_s="${3:-}"
  [ -n "$name" ] && [ -n "$min_s" ] && [ -n "$max_s" ] || usage
  is_uint "$min_s" || usage
  is_uint "$max_s" || usage
  # min must be <= max
  if [ "$min_s" -gt "$max_s" ]; then
    echo "due_gate: min_s ($min_s) > max_s ($max_s)" >&2
    exit 2
  fi

  mkdir -p "$STATE_DIR" "$LOG_DIR" 2>/dev/null || true

  # Inclusive random in [min_s, max_s] via awk (ash has no $RANDOM).
  delay=$(awk -v min="$min_s" -v max="$max_s" 'BEGIN {
    srand()
    print int(min + rand() * (max - min + 1))
  }')
  if ! is_uint "$delay"; then
    echo "due_gate: awk random failed" >&2
    exit 1
  fi

  now=$(date +%s)
  next=$((now + delay))
  stamp="$(stamp_path "$name")"
  tmp="$stamp.tmp.$$"

  # Atomic write: tmp + mv
  if ! printf '%s\n' "$next" > "$tmp"; then
    echo "due_gate: cannot write $tmp" >&2
    rm -f "$tmp" 2>/dev/null || true
    exit 1
  fi
  if ! mv "$tmp" "$stamp"; then
    echo "due_gate: cannot mv $tmp -> $stamp" >&2
    rm -f "$tmp" 2>/dev/null || true
    exit 1
  fi

  # Best-effort schedule log (CST-8 for human-readable day file).
  log="$LOG_DIR/due-gate-$(TZ=CST-8 date +%F).log"
  {
    echo "$(TZ=CST-8 date '+%F %T') schedule name=$name delay=${delay}s next=$next min=$min_s max=$max_s"
  } >> "$log" 2>/dev/null || true

  exit 0
}

main() {
  action="${1:-}"
  shift 2>/dev/null || true
  case "$action" in
    check)    cmd_check "$@" ;;
    schedule) cmd_schedule "$@" ;;
    *)        usage ;;
  esac
}

main "$@"
