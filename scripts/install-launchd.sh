#!/usr/bin/env bash
set -euo pipefail

# Installs the launchd agents — daily report (com.chat-daily-tg.agent),
# channel forwarder (com.chat-daily-tg.channels), growth + growth-weekly, and
# ledger-sync (Mac → r4s sent-content ledger push). All 5 default labels call
# their guarded wrapper, NOT python / rsync / scp directly. An optional sixth
# side-band knowledge-shadow label is installed only when explicitly enabled.
#
# Secrets (DEEPSEEK_API_KEY / TG_BOT_TOKEN / TG_CHAT_ID / GOOGLE_API_KEY / VISION_API_KEY)
# live in ~/chat-daily/.env and are loaded by run_daily at runtime — never baked into
# the plist. So this installer only renders path placeholders, no secrets.
#
# In-flight protection: if any com.chat-daily-tg.* job has a non-zero PID, abort
# by default (reload would interrupt a live fetch/send). Force with:
#   CHAT_DAILY_FORCE_RELOAD=1 ./scripts/install-launchd.sh

PROJECT="${PROJECT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
DATA_DIR="${CHAT_DAILY_DATA_DIR:-$HOME/chat-daily}"
export PROJECT DATA_DIR

mkdir -p "$HOME/Library/LaunchAgents" "$DATA_DIR/logs"

# Abort if a chat-daily-tg agent is mid-run (PID column is numeric and non-zero).
# launchctl list columns: PID Status Label  (PID is "-" when not running).
require_no_inflight() {
  local running
  running="$(
    launchctl list 2>/dev/null \
      | awk '$3 ~ /^com\.chat-daily-tg\./ && $1 ~ /^[0-9]+$/ && $1 != "0" {
          printf "  %s pid=%s\n", $3, $1
        }'
  )"
  if [ -z "$running" ]; then
    return 0
  fi
  if [ "${CHAT_DAILY_FORCE_RELOAD:-}" = "1" ]; then
    echo "⚠ in-flight com.chat-daily-tg.* jobs (CHAT_DAILY_FORCE_RELOAD=1 → forcing reload):"
    echo "$running"
    return 0
  fi
  echo "❌ in-flight com.chat-daily-tg.* jobs; aborting reload (set CHAT_DAILY_FORCE_RELOAD=1 to force):"
  echo "$running"
  exit 1
}

# Ensure the project-private bash copy the plist runs as. It is adhoc-signed so it has
# its OWN TCC identity (cdhash distinct from /bin/bash): Full Disk Access can be granted
# narrowly to just this binary, which the launchd job needs so `wx` can read the WeChat
# local DB. Built only when missing so its codesign identity — and thus the user's FDA
# grant — stays stable across reinstalls.
BASH_COPY="$PROJECT/bin/cdrun-bash"
if [ ! -x "$BASH_COPY" ]; then
  mkdir -p "$PROJECT/bin"
  cp /bin/bash "$BASH_COPY"
  codesign -f -s - "$BASH_COPY"
  chmod +x "$BASH_COPY"
  echo "✓ built project bash copy: $BASH_COPY"
  echo "  → grant it Full Disk Access (System Settings ▸ Privacy ▸ Full Disk Access) for WeChat export"
fi

# Render path placeholders (HOME / PROJECT / DATA_DIR) and (re)load one label.
install_label() {
  local label="$1"
  local src="$PROJECT/launchd/${label}.plist"
  local dst="$HOME/Library/LaunchAgents/${label}.plist"
  python3 - "$src" "$dst" <<'PY'
import os, sys, pathlib
src, dst = sys.argv[1], sys.argv[2]
text = pathlib.Path(src).read_text()
text = text.replace("REPLACE_WITH_HOME", os.environ["HOME"])
text = text.replace("REPLACE_WITH_PROJECT_DIR", os.environ["PROJECT"])
text = text.replace("REPLACE_WITH_DATA_DIR", os.environ["DATA_DIR"])
pathlib.Path(dst).write_text(text)
PY
  launchctl unload "$dst" 2>/dev/null || true
  launchctl load "$dst"
  echo "✓ launchd agent loaded: $dst"
}

require_no_inflight

install_label "com.chat-daily-tg.agent"
install_label "com.chat-daily-tg.channels"
install_label "com.chat-daily-tg.growth"
install_label "com.chat-daily-tg.growth-weekly"
install_label "com.chat-daily-tg.ledger-sync"
# B站 / YouTube digest 只在 r4s cron 上跑（run_bilibili_r4s.sh / run_youtube_r4s.sh，
# 经 bwg tinyproxy 出口）；Mac 侧 launchd 已彻底移除，此处不再安装（防双跑重复推送）。
# 订阅卡 media_sent_ledger 仍在 r4s 写出；Mac 按需显式拉取历史索引。
# ledger-sync 只向 r4s 推送 Mac sent-content ledger，不再自动拉取媒体索引。

# Knowledge shadow/rollback monitoring is deliberately opt-in until a ready
# generation has a SHADOW_CANDIDATE pointer.  Enabling this installs only the
# side-band probe/guard wrapper; it never sends Telegram or consumes updates.
if [ "${CHAT_DAILY_INSTALL_KNOWLEDGE_SHADOW:-}" = "1" ]; then
  install_label "com.chat-daily-tg.knowledge-shadow"
fi

# grep with || true so missing match doesn't abort the script
launchctl list | grep chat-daily-tg || true
