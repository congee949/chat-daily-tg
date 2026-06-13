#!/usr/bin/env bash
set -euo pipefail

# Installs the daily-report launchd agent. The plist calls scripts/run_daily_guarded.sh
# (which detects missing-.venv / non-zero exit and alerts), NOT python directly.
#
# Secrets (DEEPSEEK_API_KEY / TG_BOT_TOKEN / TG_CHAT_ID / GOOGLE_API_KEY / VISION_API_KEY)
# live in ~/chat-daily/.env and are loaded by run_daily at runtime — never baked into
# the plist. So this installer only renders path placeholders, no secrets.

PROJECT="${PROJECT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
DATA_DIR="${CHAT_DAILY_DATA_DIR:-$HOME/chat-daily}"
export PROJECT DATA_DIR
LABEL="com.chat-daily-tg.agent"
SRC="$PROJECT/launchd/${LABEL}.plist"
DST="$HOME/Library/LaunchAgents/${LABEL}.plist"

mkdir -p "$HOME/Library/LaunchAgents" "$DATA_DIR/logs"

# Render path placeholders (HOME / PROJECT / DATA_DIR). No secrets in the template.
python3 - "$SRC" "$DST" <<'PY'
import os, sys, pathlib
src, dst = sys.argv[1], sys.argv[2]
text = pathlib.Path(src).read_text()
text = text.replace("REPLACE_WITH_HOME", os.environ["HOME"])
text = text.replace("REPLACE_WITH_PROJECT_DIR", os.environ["PROJECT"])
text = text.replace("REPLACE_WITH_DATA_DIR", os.environ["DATA_DIR"])
pathlib.Path(dst).write_text(text)
PY

# Idempotent reload
launchctl unload "$DST" 2>/dev/null || true
launchctl load "$DST"

echo "✓ launchd agent loaded: $DST"
# grep with || true so missing match doesn't abort the script
launchctl list | grep chat-daily-tg || true
