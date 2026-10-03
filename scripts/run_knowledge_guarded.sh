#!/bin/bash
# Hourly KnowledgeIndex health, daily source audit, and automatic rollback guard.
#
# This wrapper is deliberately side-band: it never imports Telegram delivery
# code, consumes updates, advances markers, or writes ledgers.  launchd may fire
# every 15 minutes. Independent due gates record at most one successful hourly
# health probe and one successful daily source audit; either failure remains due
# for the next 15-minute retry without diluting the other observation stream.
set -uo pipefail

PROJECT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA_DIR="${CHAT_DAILY_DATA_DIR:-$HOME/chat-daily}"
PY="${CHAT_DAILY_PY:-$PROJECT/.venv/bin/python}"
INDEX_ROOT="${CHAT_DAILY_INDEX_ROOT:-$DATA_DIR/index}"
JOURNAL="${CHAT_DAILY_KNOWLEDGE_JOURNAL:-$INDEX_ROOT/shadow/events.jsonl}"
METRICS="${CHAT_DAILY_KNOWLEDGE_METRICS:-$INDEX_ROOT/guard/metrics.json}"
LOG="${CHAT_DAILY_KNOWLEDGE_LOG:-$DATA_DIR/logs/guard-knowledge-$(date +%F).log}"
PROXY=""
GUARD_TITLE="chat-daily-tg KnowledgeIndex 守护"
LOCK_DIR="${CHAT_DAILY_KNOWLEDGE_LOCK_DIR:-/tmp/chat-daily-tg-knowledge-shadow.lock}"
RECLAIM_DIR="${LOCK_DIR}.reclaim"
DUE_NAME="knowledge-shadow"
DUE_MIN_S="${CHAT_DAILY_KNOWLEDGE_DUE_MIN_S:-3600}"
DUE_MAX_S="${CHAT_DAILY_KNOWLEDGE_DUE_MAX_S:-3600}"
SOURCE_AUDIT_DUE_NAME="knowledge-source-audit"
SOURCE_AUDIT_DUE_MIN_S="${CHAT_DAILY_KNOWLEDGE_SOURCE_AUDIT_DUE_MIN_S:-86400}"
SOURCE_AUDIT_DUE_MAX_S="${CHAT_DAILY_KNOWLEDGE_SOURCE_AUDIT_DUE_MAX_S:-86400}"
INCREMENTAL_REFRESH_ENABLED="${CHAT_DAILY_KNOWLEDGE_INCREMENTAL_REFRESH_ENABLED:-0}"
INCREMENTAL_REFRESH_DUE_NAME="knowledge-incremental-refresh"
INCREMENTAL_REFRESH_DUE_MIN_S="${CHAT_DAILY_KNOWLEDGE_INCREMENTAL_REFRESH_DUE_MIN_S:-86400}"
INCREMENTAL_REFRESH_DUE_MAX_S="${CHAT_DAILY_KNOWLEDGE_INCREMENTAL_REFRESH_DUE_MAX_S:-86400}"
PAIRED_EVALUATION_ENABLED="${CHAT_DAILY_KNOWLEDGE_PAIRED_EVALUATION_ENABLED:-0}"
FROZEN_GOLD="${CHAT_DAILY_KNOWLEDGE_FROZEN_GOLD:-}"
EVALUATION_DUE_NAME="knowledge-paired-evaluation"
EVALUATION_DUE_MIN_S="${CHAT_DAILY_KNOWLEDGE_EVALUATION_DUE_MIN_S:-43200}"
EVALUATION_DUE_MAX_S="${CHAT_DAILY_KNOWLEDGE_EVALUATION_DUE_MAX_S:-43200}"
GUARD_MARKER="${CHAT_DAILY_KNOWLEDGE_GUARD_MARKER:-$INDEX_ROOT/guard/last-applied.sha256}"
CURRENT_EVALUATION="${CHAT_DAILY_KNOWLEDGE_GUARD_CURRENT_EVALUATION:-}"
BASELINE_EVALUATION="${CHAT_DAILY_KNOWLEDGE_GUARD_BASELINE_EVALUATION:-}"

mkdir -p "$(dirname "$LOG")" "$DATA_DIR/state" "$INDEX_ROOT/shadow" "$INDEX_ROOT/guard"

# Keep the standard wrapper contract available without enabling its Telegram
# notification or proxy paths.  This task only logs locally.
source "$PROJECT/scripts/guard_common.sh"

_log() {
  echo "$(date '+%F %T') $*" >> "$LOG"
}

_lock_path_is_safe() {
  case "$LOCK_DIR" in
    /tmp/*|"$DATA_DIR"/state/*) return 0 ;;
    *) return 1 ;;
  esac
}

_lock_release() {
  local owner
  [ ! -L "$LOCK_DIR" ] || return 0
  owner="$(cat "$LOCK_DIR/pid" 2>/dev/null || true)"
  if [ "$owner" = "$$" ]; then
    rm -f "$LOCK_DIR/pid"
    rmdir "$LOCK_DIR" 2>/dev/null || true
  fi
}

_lock_is_live() {
  local owner now mtime age
  [ -d "$LOCK_DIR" ] && [ ! -L "$LOCK_DIR" ] || return 1
  owner="$(cat "$LOCK_DIR/pid" 2>/dev/null || true)"
  case "$owner" in
    ''|*[!0-9]*)
      now="$(date +%s)"
      mtime="$(stat -f %m "$LOCK_DIR" 2>/dev/null || echo 0)"
      age=$((now - mtime))
      [ "$age" -lt 60 ]
      return
      ;;
  esac
  kill -0 "$owner" 2>/dev/null
}

_lock_acquire() {
  local owner
  if mkdir "$LOCK_DIR" 2>/dev/null; then
    echo $$ > "$LOCK_DIR/pid"
    return 0
  fi
  if [ -L "$LOCK_DIR" ]; then
    _log "unsafe symlink lock path rejected: $LOCK_DIR"
    return 1
  fi
  if _lock_is_live; then
    owner="$(cat "$LOCK_DIR/pid" 2>/dev/null || true)"
    _log "skip locked pid=${owner:-pending}"
    return 1
  fi
  if [ -L "$RECLAIM_DIR" ] || ! mkdir "$RECLAIM_DIR" 2>/dev/null; then
    _log "skip locked: reclaim in progress"
    return 1
  fi
  if _lock_is_live; then
    rmdir "$RECLAIM_DIR" 2>/dev/null || true
    _log "skip locked: owner recovered"
    return 1
  fi
  rm -f "$LOCK_DIR/pid" 2>/dev/null || true
  rmdir "$LOCK_DIR" 2>/dev/null || true
  if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    rmdir "$RECLAIM_DIR" 2>/dev/null || true
    _log "skip locked: lost reclaim race"
    return 1
  fi
  echo $$ > "$LOCK_DIR/pid"
  rmdir "$RECLAIM_DIR" 2>/dev/null || true
  return 0
}

_generation() {
  local pointer value
  if [ -n "${CHAT_DAILY_KNOWLEDGE_GENERATION:-}" ]; then
    value="$CHAT_DAILY_KNOWLEDGE_GENERATION"
  else
    pointer="$INDEX_ROOT/SHADOW_CANDIDATE"
    [ -f "$pointer" ] || pointer="$INDEX_ROOT/CURRENT"
    if [ ! -f "$pointer" ] || [ -L "$pointer" ]; then
      return 1
    fi
    IFS= read -r value < "$pointer" || true
  fi
  case "$value" in
    ''|*[!A-Za-z0-9._-]*|[!A-Za-z0-9]*) return 1 ;;
  esac
  printf '%s\n' "$value"
}

_release_pointer() {
  local name="$1" pointer value
  pointer="$INDEX_ROOT/$name"
  if [ ! -f "$pointer" ] || [ -L "$pointer" ]; then
    return 1
  fi
  IFS= read -r value < "$pointer" || true
  case "$value" in
    ''|*[!A-Za-z0-9._-]*|[!A-Za-z0-9]*) return 1 ;;
  esac
  printf '%s\n' "$value"
}

_evaluation_output_is_safe() {
  local output="$1" relative
  case "$output" in
    "$INDEX_ROOT"/guard/*)
      relative="${output#"$INDEX_ROOT"/guard/}"
      case "$relative" in
        ''|*/*|.|..) return 1 ;;
      esac
      ;;
    *) return 1 ;;
  esac
  if [ "$output" = "$METRICS" ] || [ "$output" = "$GUARD_MARKER" ]; then
    return 1
  fi
  if [ -L "$output" ] || { [ -e "$output" ] && [ ! -f "$output" ]; }; then
    return 1
  fi
}

_mark_guard_metrics() {
  local digest="$1" tmp
  if [ -e "$GUARD_MARKER" ] || [ -L "$GUARD_MARKER" ]; then
    if [ ! -f "$GUARD_MARKER" ] || [ -L "$GUARD_MARKER" ]; then
      _log "guard marker is not a regular non-symlink file: $GUARD_MARKER"
      return 1
    fi
  fi
  if [ "${#digest}" -ne 64 ]; then
    _log "refusing invalid guard metrics digest"
    return 1
  fi
  case "$digest" in
    *[!0-9a-f]*)
      _log "refusing invalid guard metrics digest"
      return 1
      ;;
  esac
  tmp="${GUARD_MARKER}.tmp.$$"
  if [ -e "$tmp" ] || [ -L "$tmp" ]; then
    _log "refusing existing guard marker temporary path: $tmp"
    return 1
  fi
  printf '%s\n' "$digest" > "$tmp" || return 1
  if ! mv "$tmp" "$GUARD_MARKER"; then
    rm -f "$tmp"
    return 1
  fi
}

if [ ! -x "$PY" ]; then
  _log "venv python missing: $PY"
  exit 1
fi
if ! _lock_path_is_safe; then
  _log "unsafe lock path rejected: $LOCK_DIR"
  exit 2
fi
case "$PAIRED_EVALUATION_ENABLED" in
  0) ;;
  1)
    [ -n "$CURRENT_EVALUATION" ] || \
      CURRENT_EVALUATION="$INDEX_ROOT/guard/evaluation-current.json"
    [ -n "$BASELINE_EVALUATION" ] || \
      BASELINE_EVALUATION="$INDEX_ROOT/guard/evaluation-baseline.json"
    ;;
  *)
    _log "paired evaluation enable flag must be 0 or 1"
    exit 2
    ;;
esac
case "$INCREMENTAL_REFRESH_ENABLED" in
  0|1) ;;
  *)
    _log "incremental refresh enable flag must be 0 or 1"
    exit 2
    ;;
esac

health_due=1
source_audit_due=1
incremental_refresh_due=0
if [ "$INCREMENTAL_REFRESH_ENABLED" = "1" ]; then
  incremental_refresh_due=1
fi
evaluation_due=0
if [ "$PAIRED_EVALUATION_ENABLED" = "1" ]; then
  evaluation_due=1
fi
if [ "${CHAT_DAILY_KNOWLEDGE_FORCE:-}" != "1" ]; then
  health_due=0
  source_audit_due=0
  incremental_refresh_due=0
  evaluation_due=0
  CHAT_DAILY_DATA_DIR="$DATA_DIR" \
    CHAT_DAILY_DUE_FALLBACK_DIR="${CHAT_DAILY_DUE_FALLBACK_DIR:-/tmp/chat-daily-due}" \
    "$PROJECT/scripts/due_gate.sh" check "$DUE_NAME"
  due_rc=$?
  if [ "$due_rc" -eq 0 ]; then
    health_due=1
  elif [ "$due_rc" -ne 1 ]; then
    _log "health due-gate check failed exit=$due_rc"
    exit "$due_rc"
  fi
  CHAT_DAILY_DATA_DIR="$DATA_DIR" \
    CHAT_DAILY_DUE_FALLBACK_DIR="${CHAT_DAILY_DUE_FALLBACK_DIR:-/tmp/chat-daily-due}" \
    "$PROJECT/scripts/due_gate.sh" check "$SOURCE_AUDIT_DUE_NAME"
  due_rc=$?
  if [ "$due_rc" -eq 0 ]; then
    source_audit_due=1
  elif [ "$due_rc" -ne 1 ]; then
    _log "source-audit due-gate check failed exit=$due_rc"
    exit "$due_rc"
  fi
  if [ "$INCREMENTAL_REFRESH_ENABLED" = "1" ]; then
    CHAT_DAILY_DATA_DIR="$DATA_DIR" \
      CHAT_DAILY_DUE_FALLBACK_DIR="${CHAT_DAILY_DUE_FALLBACK_DIR:-/tmp/chat-daily-due}" \
      "$PROJECT/scripts/due_gate.sh" check "$INCREMENTAL_REFRESH_DUE_NAME"
    due_rc=$?
    if [ "$due_rc" -eq 0 ]; then
      incremental_refresh_due=1
    elif [ "$due_rc" -ne 1 ]; then
      _log "incremental-refresh due-gate check failed exit=$due_rc"
      exit "$due_rc"
    fi
  fi
  if [ "$PAIRED_EVALUATION_ENABLED" = "1" ]; then
    CHAT_DAILY_DATA_DIR="$DATA_DIR" \
      CHAT_DAILY_DUE_FALLBACK_DIR="${CHAT_DAILY_DUE_FALLBACK_DIR:-/tmp/chat-daily-due}" \
      "$PROJECT/scripts/due_gate.sh" check "$EVALUATION_DUE_NAME"
    due_rc=$?
    if [ "$due_rc" -eq 0 ]; then
      evaluation_due=1
    elif [ "$due_rc" -ne 1 ]; then
      _log "paired-evaluation due-gate check failed exit=$due_rc"
      exit "$due_rc"
    fi
  fi
  if [ "$health_due" -eq 0 ] && [ "$source_audit_due" -eq 0 ] \
    && [ "$incremental_refresh_due" -eq 0 ] && [ "$evaluation_due" -eq 0 ]; then
    exit 0
  fi
fi

if ! _lock_acquire; then
  exit 0
fi
trap '_lock_release' EXIT INT TERM

GENERATION="$(_generation || true)"
if [ -z "$GENERATION" ]; then
  _log "no valid SHADOW_CANDIDATE or CURRENT generation pointer"
  exit 1
fi

knowledge=("$PY" -m chat_daily_tg.knowledge_cli --index-root "$INDEX_ROOT")
probe_rc=0
if [ "$health_due" -eq 1 ]; then
  probe_command=("${knowledge[@]}" shadow-probe "$GENERATION" --journal "$JOURNAL")
  if [ -n "${CHAT_DAILY_QWEN_RUNTIME_CONFIG:-}" ]; then
    probe_command+=(--runtime-config "$CHAT_DAILY_QWEN_RUNTIME_CONFIG")
  fi
  if [ -n "${CHAT_DAILY_QWEN_ENDPOINT:-}" ]; then
    probe_command+=(--endpoint "$CHAT_DAILY_QWEN_ENDPOINT")
  fi

  _log "start shadow-probe generation=$GENERATION"
  "${probe_command[@]}" >> "$LOG" 2>&1
  probe_rc=$?
  _log "end shadow-probe generation=$GENERATION exit=$probe_rc"
fi

source_audit_rc=0
if [ "$source_audit_due" -eq 1 ]; then
  source_audit_command=(
    "${knowledge[@]}" shadow-audit-sources "$GENERATION" --journal "$JOURNAL"
  )
  _log "start shadow-audit-sources generation=$GENERATION"
  "${source_audit_command[@]}" >> "$LOG" 2>&1
  source_audit_rc=$?
  _log "end shadow-audit-sources generation=$GENERATION exit=$source_audit_rc"
fi

incremental_refresh_rc=0
if [ "$incremental_refresh_due" -eq 1 ]; then
  incremental_candidate="$(_release_pointer SHADOW_CANDIDATE || true)"
  if [ -z "$incremental_candidate" ] || [ "$incremental_candidate" != "$GENERATION" ]; then
    _log "incremental refresh requires GENERATION to match regular SHADOW_CANDIDATE"
    incremental_refresh_rc=2
  else
    output_generation="${GENERATION}-incremental-$(date -u +%Y%m%dT%H%M%SZ)-$$"
    incremental_refresh_command=(
      "${knowledge[@]}" incremental-refresh "$GENERATION"
      --output-generation "$output_generation" --journal "$JOURNAL"
    )
    if [ -n "${CHAT_DAILY_QWEN_RUNTIME_CONFIG:-}" ]; then
      incremental_refresh_command+=(--runtime-config "$CHAT_DAILY_QWEN_RUNTIME_CONFIG")
    fi
    if [ -n "${CHAT_DAILY_QWEN_ENDPOINT:-}" ]; then
      incremental_refresh_command+=(--endpoint "$CHAT_DAILY_QWEN_ENDPOINT")
    fi
    _log "start incremental-refresh generation=$GENERATION output=$output_generation"
    "${incremental_refresh_command[@]}" >> "$LOG" 2>&1
    incremental_refresh_rc=$?
    _log "end incremental-refresh generation=$GENERATION output=$output_generation exit=$incremental_refresh_rc"
  fi
fi

evaluation_rc=0
if [ "$evaluation_due" -eq 1 ]; then
  staged_current="${CURRENT_EVALUATION}.tmp.$$"
  staged_baseline="${BASELINE_EVALUATION}.tmp.$$"
  current_before=""
  baseline_before=""
  gold_hash_before=""
  if [ -z "$FROZEN_GOLD" ]; then
    _log "paired evaluation requires CHAT_DAILY_KNOWLEDGE_FROZEN_GOLD"
    evaluation_rc=2
  elif [ "${FROZEN_GOLD#/}" = "$FROZEN_GOLD" ] \
    || [ ! -f "$FROZEN_GOLD" ] || [ -L "$FROZEN_GOLD" ]; then
    _log "frozen gold must be an absolute regular non-symlink file: $FROZEN_GOLD"
    evaluation_rc=2
  elif [ "$CURRENT_EVALUATION" = "$BASELINE_EVALUATION" ] \
    || ! _evaluation_output_is_safe "$CURRENT_EVALUATION" \
    || ! _evaluation_output_is_safe "$BASELINE_EVALUATION"; then
    _log "paired evaluation outputs must be distinct regular files under $INDEX_ROOT/guard"
    evaluation_rc=2
  elif [ -e "$staged_current" ] || [ -L "$staged_current" ] \
    || [ -e "$staged_baseline" ] || [ -L "$staged_baseline" ]; then
    _log "paired evaluation temporary output already exists"
    evaluation_rc=2
  else
    current_before="$(_release_pointer CURRENT || true)"
    baseline_before="$(_release_pointer PREVIOUS || true)"
    if [ -z "$current_before" ] || [ -z "$baseline_before" ] \
      || [ "$current_before" = "$baseline_before" ]; then
      _log "paired evaluation requires distinct regular CURRENT and PREVIOUS pointers"
      evaluation_rc=2
    elif ! gold_hash_before="$(/usr/bin/shasum -a 256 "$FROZEN_GOLD" | awk '{print $1}')" \
      || [ "${#gold_hash_before}" -ne 64 ]; then
      _log "failed to hash frozen gold: $FROZEN_GOLD"
      evaluation_rc=2
    else
      evaluation_command=(
        "${knowledge[@]}" evaluate "$FROZEN_GOLD"
        --generation "$current_before"
        --baseline-generation "$baseline_before"
        --output "$staged_current"
      )
      if [ -n "${CHAT_DAILY_QWEN_RUNTIME_CONFIG:-}" ]; then
        evaluation_command+=(--runtime-config "$CHAT_DAILY_QWEN_RUNTIME_CONFIG")
      fi
      if [ -n "${CHAT_DAILY_QWEN_ENDPOINT:-}" ]; then
        evaluation_command+=(--endpoint "$CHAT_DAILY_QWEN_ENDPOINT")
      fi
      _log "start paired-evaluation current=$current_before baseline=$baseline_before"
      "${evaluation_command[@]}" >> "$LOG" 2>&1
      evaluation_rc=$?
      if [ "$evaluation_rc" -eq 0 ]; then
        "$PY" -m chat_daily_tg.knowledge_eval extract-paired-baseline \
          "$staged_current" "$staged_baseline" >> "$LOG" 2>&1
        evaluation_rc=$?
      fi
      if [ "$evaluation_rc" -eq 0 ]; then
        current_after="$(_release_pointer CURRENT || true)"
        baseline_after="$(_release_pointer PREVIOUS || true)"
        gold_hash_after="$(/usr/bin/shasum -a 256 "$FROZEN_GOLD" | awk '{print $1}')"
        if [ "$current_after" != "$current_before" ] \
          || [ "$baseline_after" != "$baseline_before" ] \
          || [ "$gold_hash_after" != "$gold_hash_before" ] \
          || [ ! -f "$staged_current" ] || [ -L "$staged_current" ] \
          || [ ! -f "$staged_baseline" ] || [ -L "$staged_baseline" ]; then
          _log "paired evaluation inputs changed during measurement; receipts not refreshed"
          evaluation_rc=2
        fi
      fi
      if [ "$evaluation_rc" -eq 0 ]; then
        mkdir -p "$(dirname "$CURRENT_EVALUATION")"
        if ! mv "$staged_baseline" "$BASELINE_EVALUATION" \
          || ! mv "$staged_current" "$CURRENT_EVALUATION"; then
          _log "failed to publish paired evaluation receipts"
          evaluation_rc=2
        fi
      fi
      rm -f "$staged_current" "$staged_baseline"
      _log "end paired-evaluation current=$current_before baseline=$baseline_before exit=$evaluation_rc"
    fi
  fi
fi

guard_rc="$evaluation_rc"
if [ "$guard_rc" -eq 0 ] \
  && { [ -n "$CURRENT_EVALUATION" ] || [ -n "$BASELINE_EVALUATION" ]; }; then
  if [ -z "$CURRENT_EVALUATION" ] || [ -z "$BASELINE_EVALUATION" ]; then
    _log "guard snapshot requires both current and baseline evaluation paths"
    guard_rc=2
  else
    snapshot_command=(
      "${knowledge[@]}" guard-snapshot
      --current-evaluation "$CURRENT_EVALUATION"
      --baseline-evaluation "$BASELINE_EVALUATION"
      --shadow-journal "$JOURNAL"
      --output "$METRICS"
    )
    if [ -n "${CHAT_DAILY_QWEN_RUNTIME_CONFIG:-}" ]; then
      snapshot_command+=(--runtime-config "$CHAT_DAILY_QWEN_RUNTIME_CONFIG")
    fi
    _log "start guard-snapshot current=$CURRENT_EVALUATION baseline=$BASELINE_EVALUATION"
    "${snapshot_command[@]}" >> "$LOG" 2>&1
    guard_rc=$?
    _log "end guard-snapshot exit=$guard_rc"
  fi
fi
if [ "$guard_rc" -eq 0 ] && { [ -e "$METRICS" ] || [ -L "$METRICS" ]; }; then
  if [ ! -f "$METRICS" ] || [ -L "$METRICS" ]; then
    _log "guard metrics is not a regular non-symlink file: $METRICS"
    guard_rc=2
  elif [ -e "$GUARD_MARKER" ] || [ -L "$GUARD_MARKER" ]; then
    if [ ! -f "$GUARD_MARKER" ] || [ -L "$GUARD_MARKER" ]; then
      _log "guard marker is not a regular non-symlink file: $GUARD_MARKER"
      guard_rc=2
    fi
  fi
  if [ "$guard_rc" -eq 0 ]; then
    if ! metrics_hash="$(/usr/bin/shasum -a 256 "$METRICS" | awk '{print $1}')"; then
      _log "failed to hash guard metrics: $METRICS"
      guard_rc=2
    elif [ "${#metrics_hash}" -ne 64 ]; then
      _log "invalid guard metrics digest: $METRICS"
      guard_rc=2
    else
      case "$metrics_hash" in
        *[!0-9a-f]*)
          _log "invalid guard metrics digest: $METRICS"
          guard_rc=2
          ;;
      esac
    fi
  fi
  if [ "$guard_rc" -eq 0 ]; then
    applied=""
    if [ -f "$GUARD_MARKER" ]; then
      IFS= read -r applied < "$GUARD_MARKER" || true
    fi
    if [ -n "$applied" ]; then
      if [ "${#applied}" -ne 64 ]; then
        _log "invalid guard marker digest: $GUARD_MARKER"
        guard_rc=2
      else
        case "$applied" in
          *[!0-9a-f]*)
            _log "invalid guard marker digest: $GUARD_MARKER"
            guard_rc=2
            ;;
        esac
      fi
    fi
  fi
  if [ "$guard_rc" -eq 0 ]; then
    if [ "$metrics_hash" = "$applied" ]; then
      _log "skip guard: metrics already applied hash=$metrics_hash"
    else
      _log "start guard metrics=$METRICS hash=$metrics_hash"
      guard_command=("${knowledge[@]}" guard "$METRICS" --execute)
      if [ -n "${CHAT_DAILY_QWEN_RUNTIME_CONFIG:-}" ]; then
        guard_command+=(--runtime-config "$CHAT_DAILY_QWEN_RUNTIME_CONFIG")
      fi
      "${guard_command[@]}" >> "$LOG" 2>&1
      guard_rc=$?
      if [ "$guard_rc" -eq 0 ]; then
        after_hash="$(/usr/bin/shasum -a 256 "$METRICS" | awk '{print $1}')"
        if [ "$after_hash" != "$metrics_hash" ]; then
          _log "guard metrics changed during execution; snapshot left unmarked"
          guard_rc=2
        else
          _mark_guard_metrics "$metrics_hash" || guard_rc=$?
        fi
      fi
      _log "end guard metrics=$METRICS exit=$guard_rc"
    fi
  fi
fi

if [ "$health_due" -eq 1 ] && [ "$probe_rc" -eq 0 ]; then
  CHAT_DAILY_DATA_DIR="$DATA_DIR" \
    CHAT_DAILY_DUE_FALLBACK_DIR="${CHAT_DAILY_DUE_FALLBACK_DIR:-/tmp/chat-daily-due}" \
    "$PROJECT/scripts/due_gate.sh" schedule "$DUE_NAME" "$DUE_MIN_S" "$DUE_MAX_S" \
    >> "$LOG" 2>&1 || probe_rc=1
fi

if [ "$source_audit_due" -eq 1 ] && [ "$source_audit_rc" -eq 0 ]; then
  CHAT_DAILY_DATA_DIR="$DATA_DIR" \
    CHAT_DAILY_DUE_FALLBACK_DIR="${CHAT_DAILY_DUE_FALLBACK_DIR:-/tmp/chat-daily-due}" \
    "$PROJECT/scripts/due_gate.sh" schedule "$SOURCE_AUDIT_DUE_NAME" \
      "$SOURCE_AUDIT_DUE_MIN_S" "$SOURCE_AUDIT_DUE_MAX_S" \
    >> "$LOG" 2>&1 || source_audit_rc=1
fi

if [ "$incremental_refresh_due" -eq 1 ] && [ "$incremental_refresh_rc" -eq 0 ]; then
  CHAT_DAILY_DATA_DIR="$DATA_DIR" \
    CHAT_DAILY_DUE_FALLBACK_DIR="${CHAT_DAILY_DUE_FALLBACK_DIR:-/tmp/chat-daily-due}" \
    "$PROJECT/scripts/due_gate.sh" schedule "$INCREMENTAL_REFRESH_DUE_NAME" \
      "$INCREMENTAL_REFRESH_DUE_MIN_S" "$INCREMENTAL_REFRESH_DUE_MAX_S" \
    >> "$LOG" 2>&1 || incremental_refresh_rc=1
fi

if [ "$evaluation_due" -eq 1 ] && [ "$evaluation_rc" -eq 0 ]; then
  CHAT_DAILY_DATA_DIR="$DATA_DIR" \
    CHAT_DAILY_DUE_FALLBACK_DIR="${CHAT_DAILY_DUE_FALLBACK_DIR:-/tmp/chat-daily-due}" \
    "$PROJECT/scripts/due_gate.sh" schedule "$EVALUATION_DUE_NAME" \
      "$EVALUATION_DUE_MIN_S" "$EVALUATION_DUE_MAX_S" \
    >> "$LOG" 2>&1 || evaluation_rc=1
fi

if [ "$guard_rc" -ne 0 ]; then
  exit "$guard_rc"
fi
if [ "$source_audit_rc" -ne 0 ]; then
  exit "$source_audit_rc"
fi
if [ "$incremental_refresh_rc" -ne 0 ]; then
  exit "$incremental_refresh_rc"
fi
if [ "$evaluation_rc" -ne 0 ]; then
  exit "$evaluation_rc"
fi
exit "$probe_rc"
