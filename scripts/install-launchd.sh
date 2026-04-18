#!/usr/bin/env bash
set -euo pipefail

: "${CLIPROXY_API_KEY:?Set CLIPROXY_API_KEY env var before running}"
: "${TG_BOT_TOKEN:?Set TG_BOT_TOKEN env var before running}"
: "${TG_CHAT_ID:?Set TG_CHAT_ID env var before running}"

PROJECT=/Users/Apple/projects/wx-daily-tg
SRC="$PROJECT/launchd/com.apple.wx-daily-tg.plist"
DST="$HOME/Library/LaunchAgents/com.apple.wx-daily-tg.plist"

mkdir -p "$HOME/Library/LaunchAgents" "$HOME/wx-daily/logs"

# Render plist with secrets inlined
sed \
  -e "s|REPLACE_WITH_REAL_KEY|$CLIPROXY_API_KEY|" \
  -e "s|REPLACE_WITH_REAL_TOKEN|$TG_BOT_TOKEN|" \
  -e "s|REPLACE_WITH_REAL_CHAT_ID|$TG_CHAT_ID|" \
  "$SRC" > "$DST"

# Load (unload first to avoid "already loaded" errors)
launchctl unload "$DST" 2>/dev/null || true
launchctl load "$DST"

echo "✓ launchd agent loaded: $DST"
launchctl list | grep wx-daily-tg
