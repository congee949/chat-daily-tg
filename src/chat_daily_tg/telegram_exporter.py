from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
import re
import sqlite3
import subprocess
from zoneinfo import ZoneInfo


TG_BINARY = "tg"
LOCAL_TZ = ZoneInfo("Asia/Shanghai")
UTC = ZoneInfo("UTC")


@dataclass(frozen=True)
class ExportResult:
    group_name: str
    out_path: Path
    message_count: int
    content: str
    skipped_count: int = 0


_EMOJI_OR_SHORT_RE = re.compile(r"^[\W_]{1,8}$", re.UNICODE)


def canonical_chat_ids(chat_id: str | int) -> set[int]:
    raw = int(chat_id)
    ids = {raw, abs(raw)}
    digits = str(abs(raw))
    if digits.startswith("100") and len(digits) > 3:
        ids.add(int(digits[3:]))
    else:
        ids.add(int(f"100{digits}"))
        ids.add(-int(f"100{digits}"))
    return ids


def export_chat(
    *,
    chat_id: str,
    chat_name: str,
    since: str,
    until: str,
    out_path: Path,
    db_path: str | Path,
    limit: int = 500,
    sync_before_export: bool = True,
) -> ExportResult:
    if sync_before_export:
        sync_chat(chat_id, limit=limit)

    rows = read_messages(
        db_path=Path(db_path).expanduser(),
        chat_id=chat_id,
        since=since,
        until=until,
        limit=limit,
    )
    content_lines: list[str] = [f"# Telegram: {chat_name}", "", f"> 导出 {len(rows)} 条消息", ""]
    kept = 0
    skipped = 0
    for row in rows:
        rendered = render_message(row, fallback_chat_name=chat_name)
        if rendered is None:
            skipped += 1
            continue
        kept += 1
        content_lines.append(rendered)
        content_lines.append("")

    content_lines.append(f"> 跳过空文本/低信息消息 {skipped} 条")
    content = "\n".join(content_lines).rstrip() + "\n"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(content, encoding="utf-8")
    return ExportResult(
        group_name=chat_name,
        out_path=out_path,
        message_count=kept,
        content=content,
        skipped_count=skipped,
    )


def sync_chat(chat_id: str, limit: int) -> None:
    cmd = [TG_BINARY, "sync", "-n", str(limit), "--", str(chat_id)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise RuntimeError(f"tg sync failed for {chat_id}: {proc.stderr or proc.stdout}")


def read_messages(
    *,
    db_path: Path,
    chat_id: str,
    since: str,
    until: str,
    limit: int,
) -> list[sqlite3.Row]:
    start = datetime.combine(date.fromisoformat(since), time.min, tzinfo=LOCAL_TZ).astimezone(UTC)
    end = datetime.combine(date.fromisoformat(until), time.min, tzinfo=LOCAL_TZ).astimezone(UTC)
    ids = sorted(canonical_chat_ids(chat_id))
    placeholders = ",".join("?" for _ in ids)
    query = f"""
        SELECT chat_id, chat_name, msg_id, sender_name, content, timestamp, raw_json
        FROM messages
        WHERE chat_id IN ({placeholders})
          AND timestamp >= ?
          AND timestamp < ?
        ORDER BY timestamp ASC
        LIMIT ?
    """
    params = [*ids, start.isoformat(), end.isoformat(), limit]
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        return list(conn.execute(query, params))
    finally:
        conn.close()


def render_message(row: sqlite3.Row, *, fallback_chat_name: str) -> str | None:
    content = (row["content"] or "").strip()
    if should_skip_content(content):
        return None
    ts = parse_timestamp(row["timestamp"]).astimezone(LOCAL_TZ).strftime("%H:%M")
    sender = row["sender_name"] or "unknown"
    chat = row["chat_name"] or fallback_chat_name
    prefix = f"[Telegram / {chat} / {ts} / {sender}]"
    if row["raw_json"] and "fwd" in str(row["raw_json"]).lower():
        prefix += " [转发]"
    return f"{prefix} {content}"


def should_skip_content(content: str) -> bool:
    if not content:
        return True
    compact = content.strip()
    if len(compact) <= 2:
        return True
    if _EMOJI_OR_SHORT_RE.match(compact):
        return True
    return False


def parse_timestamp(value: str) -> datetime:
    normalized = value.replace("Z", "+00:00")
    dt = datetime.fromisoformat(normalized)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt
