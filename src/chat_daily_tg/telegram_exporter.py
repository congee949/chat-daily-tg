from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
import json
from pathlib import Path
import logging
import os
import re
import sqlite3
import subprocess
from zoneinfo import ZoneInfo
from chat_daily_tg.media import MediaCandidate, extract_telegram_media_candidates


log = logging.getLogger(__name__)
TG_BINARY = "tg"
TG_CLI_PYTHON = os.environ.get(
    "CHAT_DAILY_TG_CLI_PYTHON",
    os.path.expanduser("~/.local/share/uv/tools/kabi-tg-cli/bin/python"),
)
_PUBLIC_BACKFILL_SCRIPT = (
    Path(__file__).resolve().parents[2] / "scripts" / "tg_public_backfill.py"
)
LOCAL_TZ = ZoneInfo("Asia/Shanghai")
UTC = ZoneInfo("UTC")


class SyncManyUnsupported(RuntimeError):
    """The installed tg-cli predates the ``sync-many`` command."""


@dataclass(frozen=True)
class ExportResult:
    group_name: str
    out_path: Path
    message_count: int
    content: str
    skipped_count: int = 0
    media_candidates: list[MediaCandidate] | None = None


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
    include_patterns: Sequence[str] = (),
    exclude_senders: Sequence[str] = (),
    exclude_patterns: Sequence[str] = (),
) -> ExportResult:
    if sync_before_export:
        try:
            sync_chat(chat_id, limit=limit)
        except Exception as exc:
            # Daily analysis prefers a stale/partial local window over skipping
            # the chat. Near-month evidence: CuiMao lost 3/30 days and 电丸 2/30
            # when a failed `tg sync` aborted export. Growth already refreshes
            # 电丸's HWM; CuiMao's previous morning sync still covers ~00:00–07:05.
            log.warning(
                "tg sync failed for %s, falling back to local messages.db: %s",
                chat_id,
                exc,
            )

    rows = read_messages(
        db_path=Path(db_path).expanduser(),
        chat_id=chat_id,
        since=since,
        until=until,
        limit=limit,
    )
    included_patterns = [re.compile(pattern) for pattern in include_patterns]
    excluded_senders = set(exclude_senders)
    compiled_exclude_patterns = [re.compile(pattern) for pattern in exclude_patterns]
    included_rows = [
        row for row in rows
        if should_include_message(
            content=row["content"], include_patterns=included_patterns
        ) and not should_exclude_message(
            sender_name=row["sender_name"],
            content=row["content"],
            exclude_senders=excluded_senders,
            exclude_patterns=compiled_exclude_patterns,
        )
    ]
    filtered_count = len(rows) - len(included_rows)
    media_candidates = extract_telegram_media_candidates(
        included_rows, fallback_chat_name=chat_name
    )
    content_lines: list[str] = [f"# Telegram: {chat_name}", "", f"> 导出 {len(rows)} 条消息", ""]
    kept = 0
    skipped = filtered_count
    for row in included_rows:
        rendered = render_message(row, fallback_chat_name=chat_name)
        if rendered is None:
            skipped += 1
            continue
        kept += 1
        content_lines.append(rendered)
        content_lines.append("")

    content_lines.append(f"> 跳过空文本/低信息/来源过滤消息 {skipped} 条")
    content = "\n".join(content_lines).rstrip() + "\n"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(content, encoding="utf-8")
    return ExportResult(
        group_name=chat_name,
        out_path=out_path,
        message_count=kept,
        content=content,
        skipped_count=skipped,
        media_candidates=media_candidates,
    )


def should_include_message(
    *, content: str | None, include_patterns: Sequence[re.Pattern[str]]
) -> bool:
    """Apply an optional daily-summary allow-list before rendering or media analysis."""
    if not include_patterns:
        return True
    return any(pattern.search(content or "") for pattern in include_patterns)


def should_exclude_message(
    *,
    sender_name: str | None,
    content: str | None,
    exclude_senders: set[str],
    exclude_patterns: Sequence[re.Pattern[str]],
) -> bool:
    """Apply configured daily-summary exclusions before rendering or media analysis."""
    if sender_name in exclude_senders:
        return True
    text = content or ""
    return any(pattern.search(text) for pattern in exclude_patterns)


def sync_chat(chat_id: str, limit: int, *, db_path: str | Path | None = None,
              min_msg_id: int = 0) -> None:
    """Sync one public chat into SQLite.

    Incremental channel forwarding must fetch oldest-first from its durable HWM.
    The external ``tg sync`` command fetches newest-first and can advance its own
    DB cursor past older rows when a long-outage backlog exceeds ``limit``.
    Non-incremental callers retain the established ``tg sync`` path.
    """
    if min_msg_id > 0:
        if db_path is None:
            raise ValueError("db_path is required for incremental public backfill")
        if not os.access(TG_CLI_PYTHON, os.X_OK):
            raise RuntimeError(
                f"kabi-tg-cli python not executable at {TG_CLI_PYTHON}"
            )
        cmd = [
            TG_CLI_PYTHON,
            str(_PUBLIC_BACKFILL_SCRIPT),
            str(chat_id),
            str(Path(db_path).expanduser()),
            str(limit),
            str(min_msg_id),
        ]
    else:
        cmd = [TG_BINARY, "sync", "-n", str(limit), "--", str(chat_id)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise RuntimeError(f"tg sync failed for {chat_id}: {proc.stderr or proc.stdout}")


def sync_many_chats(
    requests: Sequence[tuple[str, int]],
    *,
    delay: float = 0,
) -> list[dict]:
    """Sync selected chats through one tg-cli process and one Telethon connection.

    This is the daily-summary fast path.  The CLI keeps the request order and
    performs each chat fetch serially, so it removes repeated Python/SQLite/
    Telethon startup without introducing concurrent session or database use.
    Runtime failures are *not* retried as individual ``tg sync`` commands here:
    callers should continue from the existing local cache, preserving the
    established stale/partial fallback and avoiding a second burst of API calls.
    """
    normalized: list[tuple[str, int]] = []
    for chat, limit in requests:
        parsed_limit = int(limit)
        if parsed_limit < 0:
            raise ValueError(f"sync limit must be >= 0 for chat {chat}")
        normalized.append((str(chat), parsed_limit))
    if not normalized:
        return []

    cmd = [TG_BINARY, "sync-many"]
    cmd.extend(f"--request={chat}={limit}" for chat, limit in normalized)
    cmd.extend(["--delay", str(delay), "--json"])
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            # Retain the old per-chat 120 s allowance while sharing startup.
            timeout=120 * len(normalized),
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"tg sync-many timed out after {exc.timeout} seconds"
        ) from exc

    diagnostic = "\n".join(part for part in (proc.stderr, proc.stdout) if part)
    if proc.returncode != 0:
        lowered = diagnostic.lower()
        if "no such command" in lowered and "sync-many" in lowered:
            raise SyncManyUnsupported("installed tg-cli has no sync-many command")
        raise RuntimeError(f"tg sync-many failed: {diagnostic.strip() or f'exit {proc.returncode}'}")
    try:
        payload = json.loads(proc.stdout)
        data = payload["data"] if isinstance(payload, dict) and "data" in payload else payload
        results = data["results"]
        if not isinstance(results, list):
            raise TypeError("results is not a list")
        if len(results) != len(normalized):
            raise ValueError("result count does not match requests")
        for result, (chat, limit) in zip(results, normalized, strict=True):
            if not isinstance(result, dict):
                raise TypeError("result item is not an object")
            if str(result.get("chat")) != chat or result.get("requested_limit") != limit:
                raise ValueError("result order or identity does not match requests")
            if result.get("status") not in {"ok", "failed", "rate_limited"}:
                raise ValueError("result status is invalid")
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("tg sync-many returned invalid structured output") from exc
    return results


def read_messages(
    *,
    db_path: Path,
    chat_id: str,
    since: str,
    until: str,
    limit: int,
    min_msg_id: int = 0,
) -> list[sqlite3.Row]:
    start = datetime.combine(date.fromisoformat(since), time.min, tzinfo=LOCAL_TZ).astimezone(UTC)
    end = datetime.combine(date.fromisoformat(until), time.min, tzinfo=LOCAL_TZ).astimezone(UTC)
    ids = sorted(canonical_chat_ids(chat_id))
    placeholders = ",".join("?" for _ in ids)
    if min_msg_id:
        # The high-water mark is the incremental cursor. Do not also apply the
        # rolling `since` lower bound: after a Telegram outage, newly synced rows
        # can be several days old but still sit above the last delivered id. A
        # date lower bound would silently discard that backlog forever once it
        # slid outside [yesterday, tomorrow).
        #
        # Keep the upper bound as a defensive guard against malformed/future
        # timestamps, and page the OLDEST ids first so the cursor cannot jump
        # over an unfetched row when more than `limit` messages accumulated.
        query = f"""
            SELECT * FROM (
                SELECT chat_id, chat_name, msg_id, sender_name, content, timestamp, raw_json
                FROM messages
                WHERE chat_id IN ({placeholders})
                  AND msg_id > ?
                  AND timestamp < ?
                ORDER BY msg_id ASC
                LIMIT ?
            ) ORDER BY timestamp ASC
        """
        params = [*ids, min_msg_id, end.isoformat(), limit]
    else:
        # Daily summary mode remains date-windowed and keeps the NEWEST `limit`;
        # selecting ASC here would silently drop the latest high-volume messages.
        query = f"""
            SELECT * FROM (
                SELECT chat_id, chat_name, msg_id, sender_name, content, timestamp, raw_json
                FROM messages
                WHERE chat_id IN ({placeholders})
                  AND timestamp >= ?
                  AND timestamp < ?
                ORDER BY timestamp DESC
                LIMIT ?
            ) ORDER BY timestamp ASC
        """
        params = [*ids, start.isoformat(), end.isoformat(), limit]
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = list(conn.execute(query, params))
    finally:
        conn.close()
    if len(rows) >= limit:
        if min_msg_id:
            log.warning("read_messages hit limit=%d for chat %s above msg_id=%d — remainder deferred to next incremental run",
                        limit, chat_id, min_msg_id)
        else:
            log.warning("read_messages hit limit=%d for chat %s [%s,%s) — older messages dropped",
                        limit, chat_id, since, until)
    return rows


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
