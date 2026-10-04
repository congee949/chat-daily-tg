"""Reserve a bounded number of Jev HTTP attempts across local processes."""
from __future__ import annotations

from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
from zoneinfo import ZoneInfo


def reserve_attempts(path: Path, *, attempts: int, daily_cap: int) -> bool:
    """Reserve before HTTP. Failed calls and crashes retain their reservation."""
    path.parent.mkdir(parents=True, exist_ok=True)
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(fd, "r+", encoding="utf-8") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        data = json.loads(stream.read() or "{}")
        count = data.get("count", 0) if data.get("date") == today else 0
        if type(count) is not int or count < 0:
            raise ValueError("invalid Jev budget state")
        if count + attempts > daily_cap:
            return False
        stream.seek(0)
        stream.truncate()
        json.dump({"date": today, "count": count + attempts}, stream)
        stream.flush()
        os.fsync(stream.fileno())
    return True
