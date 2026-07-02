from __future__ import annotations
import json
import logging
import os
import subprocess
from pathlib import Path

import httpx

from chat_daily_tg.logging_setup import redact

log = logging.getLogger(__name__)


def notify_failure(title: str, message: str) -> None:
    """Alert on a pipeline failure.

    Always shows a macOS notification (offline, no network). Additionally sends a
    best-effort Telegram message over the local http proxy when CHAT_DAILY_TG_ALERTS
    is set — so a failure on a sleeping Mac (osascript invisible) still reaches the
    user, including from the channel forwarder path (review finding #19). The flag
    keeps it from firing in tests / ad-hoc local runs. TG tokens are redacted.
    """
    title, message = redact(title), redact(message)
    _notify_macos(title, message)
    if os.environ.get("CHAT_DAILY_TG_ALERTS", "").lower() in ("1", "true", "yes"):
        _notify_telegram(f"{title}: {message}")


def _notify_macos(title: str, message: str) -> None:
    """Show a macOS notification via osascript. No-ops if osascript missing."""
    safe_title = title.replace("\\", "\\\\").replace('"', '\\"')
    safe_msg = message.replace("\\", "\\\\").replace('"', '\\"')
    script = f'display notification "{safe_msg}" with title "{safe_title}"'
    subprocess.run(["osascript", "-e", script], check=False, capture_output=True)


def _notify_telegram(text: str) -> None:
    """Best-effort Telegram alert over the local http proxy. Never raises."""
    try:
        from chat_daily_tg.paths import DATA_DIR
        env = _read_env(DATA_DIR / ".env")
        token = env.get("TG_BOT_TOKEN")
        chat_id = env.get("TG_CHAT_ID")
        if not token or not chat_id:
            return
        chat_id, thread = _alert_target(chat_id)
        proxy = os.environ.get("CHAT_DAILY_ALERT_PROXY", "http://127.0.0.1:1082")
        data = {"chat_id": chat_id, "text": f"⚠️ {text}"}
        if thread:
            data["message_thread_id"] = thread
        with httpx.Client(timeout=15.0, proxy=proxy) as client:
            client.post(f"https://api.telegram.org/bot{token}/sendMessage", data=data)
    except Exception as e:  # alerting must never break the caller
        log.warning("telegram alert failed: %s", redact(str(e)))


def _read_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip()
    except OSError:
        pass
    return out


def _alert_target(dm_chat_id: str) -> tuple[str, str]:
    """Route to the 警告/alert forum topic when configured; else the DM chat."""
    try:
        cfg = json.loads(
            (Path.home() / "qwenproxy" / ".tg-notify-targets.json").read_text(encoding="utf-8")
        )
        chat_id = str(cfg.get("chat_id") or dm_chat_id)
        thread = str((cfg.get("topics") or {}).get("alert") or "")
        return chat_id, thread
    except Exception:
        return dm_chat_id, ""
