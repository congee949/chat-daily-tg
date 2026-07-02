#!/usr/bin/env bash
set -euo pipefail

# Installs the launchd agents — the daily report (com.chat-daily-tg.agent), the
# 2-hourly channel forwarder (com.chat-daily-tg.channels), and the hourly
# Bilibili digest (com.chat-daily-tg.bilibili). All plists call their guarded
# wrapper (venv preflight + macOS/Telegram alert), NOT python directly.
#
# Secrets (DEEPSEEK_API_KEY / TG_BOT_TOKEN / TG_CHAT_ID / GOOGLE_API_KEY / VISION_API_KEY)
# live in ~/chat-daily/.env and are loaded by run_daily at runtime — never baked into
# the plist. So this installer only renders path placeholders, no secrets.

PROJECT="${PROJECT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
DATA_DIR="${CHAT_DAILY_DATA_DIR:-$HOME/chat-daily}"
export PROJECT DATA_DIR

mkdir -p "$HOME/Library/LaunchAgents" "$DATA_DIR/logs"

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

install_label "com.chat-daily-tg.agent"
install_label "com.chat-daily-tg.channels"
install_label "com.chat-daily-tg.bilibili"

# grep with || true so missing match doesn't abort the script
launchctl list | grep chat-daily-tg || true
