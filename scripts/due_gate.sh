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
# Overlay ENOSPC (r4s root ~1G) must not leave the gate open forever: tmpfs
# can still hold the next-due stamp so */5 stops hammering after a success.
FALLBACK_DIR="${CHAT_DAILY_DUE_FALLBACK_DIR:-/tmp/chat-daily-due}"

usage() {
  echo "usage: due_gate.sh check <name>" >&2
  echo "       due_gate.sh schedule <name> <min_s> <max_s>" >&2
  exit 2
}

stamp_path() {
  # $1 = name
  echo "$STATE_DIR/due-$1.next"
}

fallback_stamp_path() {
  # $1 = name
  echo "$FALLBACK_DIR/due-$1.next"
}

# Write epoch seconds atomically. $1 = dest path, $2 = value.
write_stamp() {
  dest="$1"
  value="$2"
  parent=$(dirname "$dest")
  mkdir -p "$parent" 2>/dev/null || return 1
  tmp="$dest.tmp.$$"
  if ! printf '%s\n' "$value" > "$tmp"; then
    rm -f "$tmp" 2>/dev/null || true
    return 1
  fi
  if ! mv "$tmp" "$dest"; then
    rm -f "$tmp" 2>/dev/null || true
    return 1
  fi
  return 0
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

  now=$(date +%s)
  # Any valid future stamp (primary or tmpfs fallback) means not due. A stale
  # primary left behind after ENOSPC must not override a newer fallback.
  for stamp in "$(stamp_path "$name")" "$(fallback_stamp_path "$name")"; do
    [ -f "$stamp" ] || continue
    due=$(cat "$stamp" 2>/dev/null || true)
    is_uint "$due" || continue
    found=1
    if [ "$now" -lt "$due" ]; then
      exit 1
    fi
  done

  # Missing / corrupt / all-past → due (first run, after wipe, or retry).
  exit 0
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
  fallback="$(fallback_stamp_path "$name")"

  dest="$stamp"
  if ! write_stamp "$stamp" "$next"; then
    dest="$fallback"
    if ! write_stamp "$fallback" "$next"; then
      echo "due_gate: cannot write $stamp or $fallback" >&2
      exit 1
    fi
    echo "due_gate: primary stamp unwritable, used fallback $fallback" >&2
  fi

  # Best-effort schedule log (CST-8 for human-readable day file).
  log="$LOG_DIR/due-gate-$(TZ=CST-8 date +%F).log"
  {
    echo "$(TZ=CST-8 date '+%F %T') schedule name=$name delay=${delay}s next=$next dest=$dest min=$min_s max=$max_s"
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
