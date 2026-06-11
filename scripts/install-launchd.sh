#!/usr/bin/env bash
set -euo pipefail

: "${DEEPSEEK_API_KEY:?Set DEEPSEEK_API_KEY env var before running}"
: "${TG_BOT_TOKEN:?Set TG_BOT_TOKEN env var before running}"
: "${TG_CHAT_ID:?Set TG_CHAT_ID env var before running}"

PROJECT="${PROJECT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
DATA_DIR="${CHAT_DAILY_DATA_DIR:-$HOME/chat-daily}"
export PROJECT DATA_DIR
LABEL="com.chat-daily-tg.agent"
SRC="$PROJECT/launchd/${LABEL}.plist"
DST="$HOME/Library/LaunchAgents/${LABEL}.plist"

mkdir -p "$HOME/Library/LaunchAgents" "$DATA_DIR/logs"

# Render plist with Python (safe against | or & in secrets)
python3 - "$SRC" "$DST" <<'PY'
import os, sys, pathlib
src, dst = sys.argv[1], sys.argv[2]
text = pathlib.Path(src).read_text()
for placeholder, envvar in [
    ("REPLACE_WITH_REAL_KEY", "DEEPSEEK_API_KEY"),
    ("REPLACE_WITH_REAL_TOKEN", "TG_BOT_TOKEN"),
    ("REPLACE_WITH_REAL_CHAT_ID", "TG_CHAT_ID"),
    ("REPLACE_WITH_HOME", "HOME"),
]:
    text = text.replace(placeholder, os.environ[envvar])
text = text.replace("REPLACE_WITH_PROJECT_DIR", os.environ["PROJECT"])
text = text.replace("REPLACE_WITH_DATA_DIR", os.environ["DATA_DIR"])
pathlib.Path(dst).write_text(text)
PY

# Secrets are in this file — lock permissions
chmod 600 "$DST"

# Idempotent reload
launchctl unload "$DST" 2>/dev/null || true
launchctl load "$DST"

echo "✓ launchd agent loaded: $DST"
# grep with || true so missing match doesn't abort the script
launchctl list | grep chat-daily-tg || true
