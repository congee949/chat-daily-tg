from __future__ import annotations
import subprocess


def notify_failure(title: str, message: str) -> None:
    """Show a macOS notification via osascript. No-ops if osascript missing."""
    # Escape double quotes in user text
    safe_title = title.replace("\\", "\\\\").replace('"', '\\"')
    safe_msg = message.replace("\\", "\\\\").replace('"', '\\"')
    script = f'display notification "{safe_msg}" with title "{safe_title}"'
    subprocess.run(["osascript", "-e", script], check=False, capture_output=True)
