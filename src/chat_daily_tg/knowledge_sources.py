"""Read-only adapters from ChatDaily facts into KnowledgeIndex documents."""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence
from urllib.parse import urlsplit

from chat_daily_tg.knowledge_index import (
    ArchiveMessage,
    AssetRecord,
    SourceDocument,
    SourceLink,
    SourceRepresentation,
    canonical_json,
    canonical_url,
    parse_archive_messages,
    parse_srt,
    sha256_file,
    sha256_text,
    split_archive_message_sessions,
)


@dataclass(frozen=True)
class SourcePaths:
    archive: Path
    chat_db: Path
    sent_ledger: Path
    media_ledger: Path
    podcast_root: Path
    feedback_events: Path
    feedback_reclassifications: Path

    @classmethod
    def defaults(cls, data_root: Path | None = None) -> "SourcePaths":
        data = (data_root or Path("~/chat-daily")).expanduser()
        return cls(
            archive=data / "archive",
            chat_db=data / "chat-daily.db",
            sent_ledger=data / "state" / "sent_content_ledger.jsonl",
            media_ledger=data / "state" / "media_sent_ledger.jsonl",
            podcast_root=Path("~/Projects/Podcast4Bot").expanduser(),
            feedback_events=data / "intent-feedback" / "events.jsonl",
            feedback_reclassifications=(data / "intent-feedback" / "topic_reclassifications.jsonl"),
        )


@dataclass(frozen=True)
class SourceSnapshot:
    documents: tuple[SourceDocument, ...]
    cursors: dict[str, dict[str, Any]]
    feedback_events: tuple[dict[str, Any], ...]


def _read_jsonl(path: Path, *, required_schema: str | None = None) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8", errors="strict").splitlines(), 1
    ):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"malformed JSONL {path}:{line_number}: {exc.msg}") from exc
        if not isinstance(value, dict):
            raise ValueError(f"non-object JSONL event {path}:{line_number}")
        if required_schema and value.get("schema") != required_schema:
            raise ValueError(
                f"unexpected ledger schema {path}:{line_number}: {value.get('schema')!r}"
            )
        rows.append(value)
    return rows


def _file_cursor(paths: Iterable[Path], *, base: Path) -> dict[str, Any]:
    rows = []
    for path in sorted(set(paths)):
        stat_result = path.stat()
        rows.append(
            {
                "path": path.relative_to(base).as_posix(),
                "size": stat_result.st_size,
                "sha256": sha256_file(path),
            }
        )
    return {
        "mode": "set",
        "count": len(rows),
        "set_hash": sha256_text(canonical_json(rows)),
        "items": rows,
    }


def _event_cursor(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    # Both ledgers may be atomically overwritten by their authority.  A set
    # fingerprint detects truncation/replacement without relying on byte offsets.
    hashes = sorted(sha256_text(canonical_json(row)) for row in rows)
    return {
        "mode": "set",
        "count": len(rows),
        "set_hash": sha256_text(canonical_json(hashes)),
        "event_hashes": hashes,
    }


def _with_document_ids(
    cursor: dict[str, Any], documents: Sequence[SourceDocument]
) -> dict[str, Any]:
    return {
        **cursor,
        "document_ids": sorted(document.content_id for document in documents),
    }


_WECHAT_HEADER = re.compile(r"^###\s+\d{4}-\d{2}-\d{2}\s+[0-2]\d:[0-5]\d\s*$")
_TELEGRAM_HEADER = re.compile(
    r"^\[Telegram / (?P<group>.+?) / (?P<hm>[0-2]\d:[0-5]\d) / "
    r"(?P<sender>.+?)\](?: (?P<forward>\[转发\]))?(?: (?P<body>.*))?$"
)


def _archive_role(path: Path) -> tuple[str, str] | None:
    """Return the explicit archive role, rejecting unversioned derived files."""
    lower = path.name.casefold()
    if lower.startswith("telegram-"):
        return "telegram_archive", "original"
    if lower.startswith("wechat-"):
        return "wechat_archive", "original"
    if lower in {"summary.md", "concise.md"}:
        return "daily_summary", "summary"
    return None


def _archive_parse_metadata(
    source_kind: str,
    text: str,
    *,
    parsed_messages: Sequence[ArchiveMessage] = (),
) -> dict[str, Any]:
    """Describe parser coverage so partially parsed archives never lose blocks."""
    lines = text.splitlines()
    if source_kind == "telegram_archive":
        headers = sum(1 for line in lines if _TELEGRAM_HEADER.match(line))
        parsed = len(parsed_messages)
        complete = bool(headers) and parsed == headers
        return {
            "parser_mode": "structured" if complete else "raw_fallback",
            "message_blocks": headers,
            "parsed_message_blocks": parsed,
            "parser_coverage": (parsed / headers) if headers else 0.0,
            "parser_fallback_reason": None if complete else "incomplete_message_blocks",
        }
    if source_kind != "wechat_archive":
        return {"parser_mode": "document"}

    headers = sum(1 for line in lines if _WECHAT_HEADER.match(line))
    parsed = len(parsed_messages)
    complete = bool(headers) and parsed == headers
    return {
        "parser_mode": "structured" if complete else "raw_fallback",
        "message_blocks": headers,
        "parsed_message_blocks": parsed,
        "parser_coverage": (parsed / headers) if headers else 0.0,
        "parser_fallback_reason": None if complete else "incomplete_message_blocks",
    }


def _archive_session_content_id(
    *, source_kind: str, source_ref: str, member_ids: Sequence[str]
) -> str:
    """Bind one content identity to ordered, source-scoped member identities."""
    digest = sha256_text(
        canonical_json(
            {
                "source_kind": source_kind,
                "source_ref": source_ref,
                "member_ids": list(member_ids),
            }
        )
    )
    return f"archive-session:v1:{digest}"


def _render_archive_session(source_kind: str, messages: Sequence[ArchiveMessage]) -> str:
    """Render normalized text accepted by the shared conversation chunker."""
    blocks: list[str] = []
    for message in messages:
        if source_kind == "telegram_archive":
            body_lines = message.body.splitlines()
            first = body_lines[0] if body_lines else ""
            header = (
                f"[Telegram / archive / {message.timestamp.strftime('%H:%M')} / "
                f"{message.sender}] {first}"
            ).rstrip()
            blocks.append("\n".join((header, *body_lines[1:])))
            continue
        body_lines = message.body.splitlines()
        first = body_lines[0] if body_lines else ""
        sender_line = f"**{message.sender}**: {first}".rstrip()
        blocks.append(
            "\n".join(
                (
                    f"### {message.timestamp.strftime('%Y-%m-%d %H:%M')}",
                    "",
                    sender_line,
                    *body_lines[1:],
                )
            )
        )
    return "\n\n".join(blocks)


def _ordered_event_cursor(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    hashes = [sha256_text(canonical_json(row)) for row in rows]
    return {
        "mode": "append_ordered",
        "count": len(hashes),
        "last_event_hash": hashes[-1] if hashes else None,
        "sequence_hash": sha256_text(canonical_json(hashes)),
    }


def load_archive(path: Path) -> tuple[list[SourceDocument], dict[str, Any]]:
    if not path.is_dir():
        return [], {
            "mode": "set",
            "count": 0,
            "set_hash": sha256_text("[]"),
            "items": [],
            "document_ids": [],
        }
    files = [item for item in path.rglob("*.md") if _archive_role(item) is not None]
    documents: list[SourceDocument] = []
    for item in sorted(files):
        relative = item.relative_to(path).as_posix()
        text = item.read_text(encoding="utf-8", errors="strict").strip()
        if not text:
            continue
        role_info = _archive_role(item)
        assert role_info is not None
        source_kind, role = role_info
        title_match = re.search(r"^#\s+(.+)$", text, re.MULTILINE)
        title = title_match.group(1).strip() if title_match else item.stem
        published = "-".join(item.relative_to(path).parts[:3])
        parsed_messages = parse_archive_messages(
            source_kind=source_kind,
            source_ref=relative,
            text=text,
            published_at=published,
        )
        parse_metadata = _archive_parse_metadata(
            source_kind,
            text,
            parsed_messages=parsed_messages,
        )
        if source_kind == "telegram_archive" and not parse_metadata["message_blocks"]:
            # Header-only zero-message exports and derived files that merely
            # happen to start with telegram- are not semantic source content.
            # They remain in the source cursor so later real messages trigger
            # a rebuild, but they do not become dense documents.
            continue
        common_metadata = {
            "relative_path": relative,
            "archive_source_kind": source_kind,
            **parse_metadata,
        }
        if (
            source_kind in {"wechat_archive", "telegram_archive"}
            and parse_metadata["parser_mode"] == "structured"
        ):
            sessions = split_archive_message_sessions(parsed_messages, gap_seconds=600)
            for session_index, session in enumerate(sessions, 1):
                member_ids = [message.member_id for message in session]
                documents.append(
                    SourceDocument(
                        content_id=_archive_session_content_id(
                            source_kind=source_kind,
                            source_ref=relative,
                            member_ids=member_ids,
                        ),
                        source_kind=source_kind,
                        source_ref=relative,
                        text=_render_archive_session(source_kind, session),
                        title=title,
                        published_at=published,
                        producer="chatdaily",
                        authority="archive",
                        mapping_status="source_only",
                        metadata={
                            **common_metadata,
                            "identity_scope": "message_session",
                            "identity_reason": "structured_messages_gap_gt_10m",
                            "source_file_content_id": f"archive:{relative}",
                            "session_index": session_index,
                            "session_count": len(sessions),
                            "session_start": session[0].timestamp.isoformat(),
                            "session_end": session[-1].timestamp.isoformat(),
                            "member_ids": member_ids,
                            "member_count": len(member_ids),
                        },
                        document_role=role,
                        representation_type="markdown",
                    )
                )
            continue

        is_raw_fallback = source_kind in {"wechat_archive", "telegram_archive"}
        documents.append(
            SourceDocument(
                content_id=f"archive:{relative}",
                source_kind=f"{source_kind}_raw" if is_raw_fallback else source_kind,
                source_ref=relative,
                text=text,
                title=title,
                published_at=published,
                producer="chatdaily",
                authority="archive",
                mapping_status="source_only",
                metadata={
                    **common_metadata,
                    "identity_scope": "file",
                    "identity_reason": (
                        "raw_fallback:incomplete_or_unparseable_message_blocks"
                        if is_raw_fallback
                        else "summary_file_level_by_design"
                    ),
                },
                document_role=role,
                representation_type="markdown_raw" if is_raw_fallback else "markdown",
            )
        )
    return documents, _with_document_ids(_file_cursor(files, base=path), documents)


def load_chat_db(path: Path) -> tuple[list[SourceDocument], dict[str, Any]]:
    if not path.is_file():
        return [], {"mode": "sqlite", "exists": False, "document_ids": []}
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    documents: list[SourceDocument] = []
    specs = {
        "permanent": (
            "title",
            ("content", "notes"),
            "captured_at",
            "source_group",
            "source_sender",
        ),
        "hot_leads": (
            "title",
            ("summary", "risk_notes"),
            "captured_at",
            "source_group",
            "source_sender",
        ),
        "repeat_topics": (
            "title",
            ("last_summary", "last_new_information"),
            "last_seen",
            "last_source_group",
            "last_source_sender",
        ),
    }
    counts: dict[str, int] = {}
    semantic_rows: dict[str, list[str]] = {}
    schema_rows: list[dict[str, str]] = []
    try:
        # Keep all tables and the cursor on one WAL-aware SQLite snapshot.
        conn.execute("BEGIN")
        existing = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        schema_rows = [
            {"name": str(row[0]), "sql": str(row[1] or "")}
            for row in conn.execute(
                "SELECT name, sql FROM sqlite_master "
                "WHERE type='table' AND name IN ('permanent','hot_leads','repeat_topics') "
                "ORDER BY name"
            )
        ]
        for table, spec in specs.items():
            title_column, body_columns, time_column, group_column, sender_column = spec
            if table not in existing:
                counts[table] = 0
                semantic_rows[table] = []
                continue
            rows = conn.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
            counts[table] = len(rows)
            semantic_rows[table] = [
                sha256_text(canonical_json({key: row[key] for key in row.keys()})) for row in rows
            ]
            for row in rows:
                body = "\n\n".join(
                    str(row[column]).strip()
                    for column in body_columns
                    if column in row.keys() and row[column]
                )
                title = str(row[title_column] or "").strip()
                if not (title or body):
                    continue
                source_ref = "/".join(
                    value
                    for value in (
                        str(row[group_column] or "").strip(),
                        str(row[sender_column] or "").strip(),
                    )
                    if value
                )
                status = str(row["status"] or "") if "status" in row.keys() else ""
                documents.append(
                    SourceDocument(
                        content_id=f"chat-db:{table}:{row['id']}",
                        source_kind=f"chat_db_{table}",
                        source_ref=source_ref or table,
                        text=f"{title}\n\n{body}".strip(),
                        title=title,
                        published_at=str(row[time_column] or ""),
                        canonical_url=str(row["url"] or "") if "url" in row.keys() else "",
                        producer="chatdaily",
                        delivery_state=status,
                        authority="chat-daily.db",
                        mapping_status="source_only",
                        metadata={"table": table, "row_id": str(row["id"]), "status": status},
                        document_role="derived_business_entity",
                        representation_type="database_record",
                    )
                )
    finally:
        conn.close()
    cursor = {
        "mode": "sqlite_snapshot",
        "tables": counts,
        "schema_hash": sha256_text(canonical_json(schema_rows)),
        "set_hash": sha256_text(canonical_json(semantic_rows)),
        "document_ids": sorted(document.content_id for document in documents),
    }
    return documents, cursor


def _ledger_link(row: dict[str, Any], schema: str) -> SourceLink:
    try:
        return SourceLink(
            chat_id=int(row["chat_id"]),
            thread_id=int(row["thread_id"]) if row.get("thread_id") is not None else None,
            message_id=int(row["message_id"]),
            source_message_id=(
                int(row["source_message_ids"][0])
                if isinstance(row.get("source_message_ids"), list) and row["source_message_ids"]
                else None
            ),
            ledger_schema=schema,
            confirmed=str(row.get("delivery_state") or "confirmed") == "confirmed",
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("ledger row has invalid delivery mapping") from exc


def _sent_identity(row: dict[str, Any]) -> dict[str, Any]:
    content_id = str(row.get("content_id") or "").strip()
    content = row.get("content")
    content_hash = str(row.get("content_hash") or "").strip()
    producer = str(row.get("producer") or "").strip()
    source_kind = str(row.get("source_kind") or "").strip()
    source_ref = str(row.get("source_ref") or "").strip()
    source_values = row.get("source_message_ids")
    url = canonical_url(str(row.get("url") or ""))
    if not content_id or not isinstance(content, str) or not content.strip():
        raise ValueError("sent-content ledger row is missing content identity/text")
    if not producer or not source_kind or not source_ref or not url:
        raise ValueError("sent-content ledger row is missing provenance")
    if not isinstance(source_values, list) or not source_values:
        raise ValueError("sent-content ledger row has no source_message_ids")
    try:
        source_ids = tuple(int(value) for value in source_values)
    except (TypeError, ValueError) as exc:
        raise ValueError("sent-content ledger row has invalid source_message_ids") from exc
    if any(value <= 0 for value in source_ids):
        raise ValueError("sent-content ledger row has invalid source_message_ids")
    if content_hash != sha256_text(content):
        raise ValueError(f"sent-content hash mismatch for {content_id}")
    return {
        "content_id": content_id,
        "content": content,
        "content_hash": content_hash,
        "producer": producer,
        "source_kind": source_kind,
        "source_ref": source_ref,
        "source_message_ids": source_ids,
        "url": url,
    }


def load_sent_ledger(path: Path) -> tuple[list[SourceDocument], dict[str, Any]]:
    rows = _read_jsonl(path, required_schema="sent-content.v1")
    seen_delivery_links: dict[tuple[int, int], SourceLink] = {}
    seen_delivery_targets: dict[tuple[int, int], tuple[str, str, str]] = {}
    grouped: dict[str, list[tuple[dict[str, Any], dict[str, Any], SourceLink]]] = {}
    identities: dict[str, dict[str, Any]] = {}
    for row in rows:
        link = _ledger_link(row, "sent-content.v1")
        if link.message_id <= 0 or (link.thread_id is not None and link.thread_id <= 0):
            raise ValueError("sent-content ledger row has invalid message/thread id")
        key = (link.chat_id, link.message_id)
        previous_link = seen_delivery_links.setdefault(key, link)
        if previous_link != link:
            raise ValueError(f"sent-content authority conflict for delivery link {key}")
        identity = _sent_identity(row)
        content_id = identity["content_id"]
        prior_identity = identities.setdefault(content_id, identity)
        if prior_identity != identity:
            raise ValueError(f"sent-content authority conflict for content {content_id}")
        target_identity = (content_id, identity["url"], identity["producer"])
        previous = seen_delivery_targets.setdefault(key, target_identity)
        if previous != target_identity:
            raise ValueError(f"sent-content authority conflict for delivery {key}")
        grouped.setdefault(content_id, []).append((row, identity, link))

    documents: list[SourceDocument] = []
    for content_id, group in grouped.items():
        first_row, identity, _first_link = group[0]
        links: list[SourceLink] = []
        seen_links: set[tuple[int, int]] = set()
        for _row, _identity, link in group:
            key = (link.chat_id, link.message_id)
            if key not in seen_links:
                links.append(link)
                seen_links.add(key)
        confirmed = all(
            str(row.get("delivery_state") or "") == "confirmed" for row, _identity, _link in group
        )
        documents.append(
            SourceDocument(
                content_id=content_id,
                source_kind=identity["source_kind"],
                source_ref=identity["source_ref"],
                text=identity["content"].strip(),
                title="",
                published_at=str(first_row.get("sent_at") or ""),
                canonical_url=identity["url"],
                producer=identity["producer"],
                delivery_state="confirmed" if confirmed else "mixed",
                authority="sent-content.v1",
                mapping_status="confirmed" if confirmed else "pending",
                metadata={
                    "member_content_ids": [content_id],
                    "member_count": 1,
                    "source_message_ids": list(identity["source_message_ids"]),
                    "delivery_count": len(links),
                    "ledger_content_hash": identity["content_hash"],
                },
                document_role="delivered_original",
                representation_type="ledger_content",
                source_links=links,
            )
        )
    cursor = _with_document_ids(_event_cursor(rows), documents)
    cursor.update(
        {
            "schema": "sent-content.v1",
            "delivery_ids": sorted(
                f"{link.chat_id}:{link.message_id}"
                for document in documents
                for link in document.source_links
            ),
        }
    )
    return documents, cursor


_MEDIA_LEDGER_REQUIRED_FIELDS = {
    "chat_id",
    "message_id",
    "id",
    "url",
    "producer",
    "ts",
}
_MEDIA_LEDGER_OPTIONAL_FIELDS = {"thread_id", "schema"}
_MEDIA_LEDGER_LEGACY_SCHEMA = "media-sent.legacy-v1"
_MEDIA_LEDGER_SCHEMA = "media-sent.v1"


def _media_row_schema(row: dict[str, Any]) -> str:
    schema = row.get("schema")
    if schema is None:
        return _MEDIA_LEDGER_LEGACY_SCHEMA
    if schema != _MEDIA_LEDGER_SCHEMA:
        raise ValueError(f"unsupported media ledger schema: {schema!r}")
    return _MEDIA_LEDGER_SCHEMA


def _media_int(row: dict[str, Any], field: str) -> int:
    value = row.get(field)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"media ledger {field} must be an integer")
    if not -(2**63) <= value < 2**63:
        raise ValueError(f"media ledger {field} is outside int64 range")
    return value


def _validate_media_row(row: dict[str, Any]) -> str:
    fields = set(row)
    missing = _MEDIA_LEDGER_REQUIRED_FIELDS - fields
    if missing:
        raise ValueError(f"media ledger row is missing fields: {sorted(missing)}")
    unknown = fields - _MEDIA_LEDGER_REQUIRED_FIELDS - _MEDIA_LEDGER_OPTIONAL_FIELDS
    if unknown:
        raise ValueError(f"media ledger row has unknown fields: {sorted(unknown)}")
    schema = _media_row_schema(row)
    chat_id = _media_int(row, "chat_id")
    message_id = _media_int(row, "message_id")
    thread_id = _media_int(row, "thread_id") if row.get("thread_id") is not None else None
    if chat_id == 0 or message_id <= 0 or (thread_id is not None and thread_id <= 0):
        raise ValueError("media ledger row has invalid delivery mapping")
    for field in ("id", "producer"):
        if not isinstance(row[field], str) or not row[field].strip():
            raise ValueError(f"media ledger {field} must be a non-empty string")
    url = row["url"]
    if not isinstance(url, str):
        raise ValueError("media ledger url must be a string")
    parts = urlsplit(url.strip())
    if parts.scheme.casefold() not in {"http", "https"} or not parts.netloc:
        raise ValueError("media ledger url must be an absolute HTTP(S) URL")
    timestamp = row["ts"]
    if not isinstance(timestamp, str) or not timestamp.strip():
        raise ValueError("media ledger ts must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(timestamp.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("media ledger ts must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("media ledger ts must include a timezone")
    return schema


def load_media_ledger(path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows = _read_jsonl(path)
    unique_rows: list[dict[str, Any]] = []
    by_delivery: dict[
        tuple[int, int], tuple[str, str, str, int | None, str, str]
    ] = {}
    by_content: dict[str, tuple[str, str]] = {}
    by_url: dict[str, tuple[str, str]] = {}
    schemas: set[str] = set()
    for row in rows:
        schema = _validate_media_row(row)
        schemas.add(schema)
        chat_id = _media_int(row, "chat_id")
        message_id = _media_int(row, "message_id")
        thread_id = _media_int(row, "thread_id") if row.get("thread_id") is not None else None
        key = (chat_id, message_id)
        content_id = row["id"].strip()
        url = canonical_url(row["url"])
        producer = row["producer"].strip()
        delivery_authority = (
            content_id,
            url,
            producer,
            thread_id,
            schema,
            row["ts"].strip(),
        )
        previous = by_delivery.get(key)
        if previous is None:
            by_delivery[key] = delivery_authority
            unique_rows.append(row)
        elif previous != delivery_authority:
            raise ValueError(f"media ledger authority conflict for delivery {key}")
        prior_content = by_content.setdefault(content_id, (url, producer))
        if prior_content != (url, producer):
            raise ValueError(f"media ledger authority conflict for content {content_id}")
        prior_url = by_url.setdefault(url, (content_id, producer))
        if prior_url != (content_id, producer):
            raise ValueError("media ledger URL maps to conflicting content authority")
    if len(schemas) > 1:
        raise ValueError("media ledger mixes incompatible schemas")
    cursor = _event_cursor(rows)
    cursor.update(
        {
            "schema": next(iter(schemas), None),
            "delivery_ids": sorted(
                f"{chat_id}:{message_id}" for chat_id, message_id in by_delivery
            ),
            "content_ids": sorted(by_content),
        }
    )
    return unique_rows, cursor


def _media_links(
    rows: Sequence[dict[str, Any]], url: str, content_id: str, producer: str
) -> list[SourceLink]:
    canonical = canonical_url(url)
    ledger_producer = _ledger_producer(producer)
    output: list[SourceLink] = []
    for row in rows:
        if not (
            canonical_url(str(row.get("url") or "")) == canonical
            and str(row.get("id") or "").strip() == content_id
            and str(row.get("producer") or "").strip() == ledger_producer
        ):
            continue
        output.append(
            SourceLink(
                chat_id=int(row["chat_id"]),
                thread_id=int(row["thread_id"]) if row.get("thread_id") is not None else None,
                message_id=int(row["message_id"]),
                source_message_id=None,
                ledger_schema=_media_row_schema(row),
                confirmed=True,
            )
        )
    return output


def _ledger_producer(producer: str) -> str:
    # The subscription ledger uses one Bilibili producer for both videos and
    # articles; Podcast metadata keeps the narrower article adapter name.
    return "bilibili" if producer == "bilibili_article" else producer


def _media_id(
    rows: Sequence[dict[str, Any]], url: str, producer: str, key: str
) -> tuple[str, bool]:
    canonical = canonical_url(url)
    matching_url = [
        row for row in rows if canonical and canonical_url(str(row.get("url") or "")) == canonical
    ]
    if not matching_url:
        material = f"{producer}|{canonical or f'cache-key:{key}'}"
        return "podcast:v1:" + sha256_text(material), False
    ledger_producer = _ledger_producer(producer)
    matching_producer = [
        row for row in matching_url if str(row.get("producer") or "").strip() == ledger_producer
    ]
    if not matching_producer:
        raise ValueError("Podcast4Bot producer contradicts media ledger authority")
    ids = {str(row.get("id") or "").strip() for row in matching_producer}
    if len(ids) > 1:
        raise ValueError("media ledger URL maps to multiple content ids")
    content_id = next(iter(ids))
    if not content_id:
        raise ValueError("media ledger URL maps to an empty content id")
    return content_id, True


def _strict_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="strict")


def _srt_is_monotonic(text: str) -> tuple[bool, list[tuple[int, int, str]]]:
    cues = parse_srt(text)
    previous_start = -1
    for start, end, _body in cues:
        if start < previous_start or end < start:
            return False, cues
        previous_start = start
    return bool(cues), cues


def load_podcast(
    root: Path, media_rows: Sequence[dict[str, Any]]
) -> tuple[list[SourceDocument], dict[str, Any]]:
    if not root.is_dir():
        return [], {
            "mode": "set",
            "count": 0,
            "set_hash": sha256_text("[]"),
            "items": [],
            "document_ids": [],
        }
    meta_files = sorted([*root.glob("transcripts/*.meta.json"), *root.glob("articles/*.meta.json")])
    cursor_files: set[Path] = set(meta_files)
    selected: dict[str, dict[str, tuple[int, str, SourceDocument]]] = {}
    media_rows_by_url: dict[str, list[dict[str, Any]]] | None = None
    for meta_path in meta_files:
        try:
            metadata = json.loads(meta_path.read_text(encoding="utf-8", errors="strict"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError(f"invalid Podcast4Bot metadata: {meta_path}") from exc
        if not isinstance(metadata, dict):
            raise ValueError(f"Podcast4Bot metadata is not an object: {meta_path}")
        key = str(metadata.get("key") or meta_path.name.removesuffix(".meta.json"))
        if metadata.get("key") and key != meta_path.name.removesuffix(".meta.json"):
            raise ValueError("Podcast4Bot metadata key contradicts its filename")
        url = canonical_url(str(metadata.get("url") or ""))
        producer = str(metadata.get("platform") or "Podcast4Bot").strip()
        # Build once, lazily: empty/source-only caches previously never parsed
        # ledger URLs. Preserve that boundary and all helper authority checks.
        if url and media_rows_by_url is None:
            media_rows_by_url = {}
            for row in media_rows:
                row_url = canonical_url(str(row.get("url") or ""))
                media_rows_by_url.setdefault(row_url, []).append(row)
        matching_media_rows = (media_rows_by_url or {}).get(url, []) if url else []
        content_id, media_confirmed = _media_id(matching_media_rows, url, producer, key)
        base = meta_path.with_name(key)
        srt_path = base.with_suffix(".srt")
        txt_path = base.with_suffix(".txt")
        fallback_reason = ""
        if srt_path.is_file():
            srt_text = _strict_text(srt_path)
            cursor_files.add(srt_path)
            monotonic, cues = _srt_is_monotonic(srt_text)
            if monotonic:
                text = srt_text.strip()
                representation = "srt"
            elif txt_path.is_file():
                text = _strict_text(txt_path).strip()
                cursor_files.add(txt_path)
                representation = "transcript"
                fallback_reason = "nonmonotonic_or_invalid_srt"
            elif cues:
                text = " ".join(body for _start, _end, body in cues).strip()
                representation = "transcript"
                fallback_reason = "nonmonotonic_srt_without_txt"
            else:
                text = str(metadata.get("description") or "").strip()
                representation = "metadata_description"
                fallback_reason = "invalid_srt_without_txt"
        elif txt_path.is_file():
            text = _strict_text(txt_path).strip()
            cursor_files.add(txt_path)
            if meta_path.parent.name == "transcripts":
                representation = "transcript"
            elif metadata.get("media_modality") == "gallery":
                representation = "vision_text"
            else:
                representation = "article"
        else:
            text = str(metadata.get("description") or "").strip()
            representation = "metadata_description"
        if not text:
            continue
        text_hash = sha256_text(text)
        assets: list[AssetRecord] = []
        gallery_paths = metadata.get("gallery_paths")
        gallery_hashes = metadata.get("gallery_sha256")
        if isinstance(gallery_paths, list):
            for index, local_ref in enumerate(gallery_paths):
                digest = (
                    str(gallery_hashes[index])
                    if isinstance(gallery_hashes, list) and index < len(gallery_hashes)
                    else ""
                )
                asset_path = Path(str(local_ref)).expanduser()
                if not asset_path.is_absolute():
                    asset_path = root / asset_path
                if digest:
                    if not asset_path.is_file():
                        raise ValueError("Podcast4Bot gallery asset is missing")
                    if sha256_file(asset_path) != digest:
                        raise ValueError("Podcast4Bot gallery asset hash mismatch")
                    try:
                        asset_path.relative_to(root)
                    except ValueError:
                        pass
                    else:
                        cursor_files.add(asset_path)
                assets.append(
                    AssetRecord(
                        asset_id=f"{content_id}:asset:{key}:{index + 1}",
                        sha256=digest,
                        local_ref=str(local_ref),
                        vision_json={
                            "visual_evidence_verified": metadata.get("visual_evidence_verified")
                        },
                    )
                )
        source_kind = (
            "podcast_transcript" if meta_path.parent.name == "transcripts" else "podcast_article"
        )
        links = _media_links(matching_media_rows, url, content_id, producer) if media_confirmed else []
        if media_confirmed and not links:
            raise ValueError("Podcast4Bot media authority resolved without a delivery link")
        document_role = "metadata" if representation == "metadata_description" else "original"
        document = SourceDocument(
            content_id=content_id,
            source_kind=source_kind,
            source_ref=url or meta_path.relative_to(root).as_posix(),
            text=text,
            title=str(metadata.get("title") or ""),
            canonical_url=url,
            producer=producer,
            authority="Podcast4Bot",
            mapping_status="confirmed" if links else "source_only",
            metadata={
                "key": key,
                "channel": metadata.get("channel"),
                "duration": metadata.get("duration"),
                "asr_engine": metadata.get("asr_engine"),
                "asr_model": metadata.get("asr_model"),
                "asr_quality": metadata.get("asr_quality"),
                "media_modality": metadata.get("media_modality"),
                "srt_fallback_reason": fallback_reason or None,
            },
            document_role=document_role,
            modality="text",
            representation_type=representation,
            source_links=links,
            assets=assets,
        )
        # Producer caches can legitimately contain multiple representations of
        # one stable content item. Preserve each as chunks under that one
        # content_id so the reader's per-content cap prevents duplicate ranking
        # occupancy. Divergent artifacts of the same type remain an authority
        # error.
        priority = {
            "vision_text": 50,
            "article": 40,
            "srt": 30,
            "transcript": 20,
        }.get(representation, 10)
        representations = selected.setdefault(content_id, {})
        previous = representations.get(representation)
        if previous is None:
            representations[representation] = (priority, text_hash, document)
        elif text_hash != previous[1]:
            raise ValueError(f"Podcast4Bot content conflict for {content_id}")
    documents: list[SourceDocument] = []
    for content_id, representations in selected.items():
        ordered = sorted(
            representations.values(),
            key=lambda value: (-value[0], value[2].representation_type, value[1]),
        )
        primary = ordered[0][2]
        links: list[SourceLink] = []
        seen_links: set[str] = set()
        assets: list[AssetRecord] = []
        seen_assets: set[str] = set()
        representation_inventory: list[dict[str, Any]] = []
        alternates: list[SourceRepresentation] = []
        for _priority, text_hash, value in ordered:
            for link in value.source_links:
                key = canonical_json(link.__dict__)
                if key not in seen_links:
                    links.append(link)
                    seen_links.add(key)
            for asset in value.assets:
                if asset.asset_id not in seen_assets:
                    assets.append(asset)
                    seen_assets.add(asset.asset_id)
            locator = (
                f"{value.source_kind}:{value.metadata.get('key') or value.source_ref}:"
                f"{value.representation_type}"
            )
            representation_inventory.append(
                {
                    "locator": locator,
                    "text_hash": text_hash,
                    "source_kind": value.source_kind,
                    "source_ref": value.source_ref,
                    "document_role": value.document_role,
                    "representation_type": value.representation_type,
                }
            )
            if value is not primary:
                alternates.append(
                    SourceRepresentation(
                        text=value.text,
                        document_role=value.document_role,
                        representation_type=value.representation_type,
                        modality=value.modality,
                        source_kind=value.source_kind,
                        source_ref=value.source_ref,
                        title=value.title,
                        locator=locator,
                    )
                )
        primary.metadata = {
            **primary.metadata,
            "representations": representation_inventory,
        }
        primary.source_links = links
        primary.assets = assets
        primary.alternate_representations = tuple(alternates)
        documents.append(primary)
    return documents, _with_document_ids(_file_cursor(cursor_files, base=root), documents)


def load_feedback(
    path: Path,
    reclassifications_path: Path | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows = _read_jsonl(path, required_schema="intent-feedback.v1")
    corrections = _read_jsonl(
        reclassifications_path or path.parent / "topic_reclassifications.jsonl",
        required_schema="intent-feedback.topic-reclassification.v1",
    )
    cursor = {
        "mode": "feedback_projection",
        "events": _event_cursor(rows),
        "reclassifications": _ordered_event_cursor(corrections),
    }
    seen: dict[str, str] = {}
    emitted: set[str] = set()
    pending: list[dict[str, Any]] = []
    for row in rows:
        event_id = str(row.get("event_id") or "").strip()
        if not event_id:
            raise ValueError("intent feedback event has no event_id")
        event_hash = sha256_text(canonical_json(row))
        previous = seen.setdefault(event_id, event_hash)
        if previous != event_hash:
            raise ValueError(f"intent feedback authority conflict for event {event_id}")
        if event_id in emitted:
            continue
        item = dict(row)
        delivery_content_id = str(row.get("content_id") or "").strip()
        if not delivery_content_id:
            raise ValueError(f"intent feedback event {event_id} has no content_id")
        item["delivery_content_id"] = delivery_content_id
        source = row.get("source") if isinstance(row.get("source"), dict) else {}
        target = row.get("target") if isinstance(row.get("target"), dict) else {}
        delivery_confirmed = (
            source.get("mapping_status") == "confirmed"
            and target.get("mapping_status") == "confirmed"
        )
        item["delivery_mapping_status"] = "confirmed" if delivery_confirmed else "pending"
        # Delivery confirmation alone does not identify a KnowledgeIndex item.
        item["mapping_status"] = "pending"
        item["confirmed"] = False
        pending.append(item)
        emitted.add(event_id)

    by_event = {str(item["event_id"]): item for item in pending}
    original_topics: dict[str, dict[str, Any]] = {}
    seen_corrections: dict[str, str] = {}
    for correction in corrections:
        correction_id = str(correction.get("reclassification_id") or "").strip()
        event_id = str(correction.get("event_id") or "").strip()
        if not correction_id or not event_id:
            raise ValueError("topic reclassification requires reclassification_id and event_id")
        correction_hash = sha256_text(canonical_json(correction))
        previous = seen_corrections.get(correction_id)
        if previous is not None:
            if previous != correction_hash:
                raise ValueError(f"topic reclassification authority conflict for {correction_id}")
            # Byte-equivalent retries are idempotent even if an interrupted
            # append left a duplicate row.
            continue
        seen_corrections[correction_id] = correction_hash
        item = by_event.get(event_id)
        if item is None:
            raise ValueError(
                f"topic reclassification {correction_id} references unknown event {event_id}"
            )
        current = item.get("topic") if isinstance(item.get("topic"), dict) else None
        old_topic = (
            correction.get("old_topic") if isinstance(correction.get("old_topic"), dict) else None
        )
        new_topic = (
            correction.get("new_topic") if isinstance(correction.get("new_topic"), dict) else None
        )
        for name, topic in (
            ("current", current),
            ("old_topic", old_topic),
            ("new_topic", new_topic),
        ):
            if (
                not topic
                or not str(topic.get("label") or "").strip()
                or not str(topic.get("slug") or "").strip()
            ):
                raise ValueError(f"topic reclassification {correction_id} has invalid {name}")
        assert current is not None and old_topic is not None and new_topic is not None
        current_identity = (str(current["label"]), str(current["slug"]))
        old_identity = (str(old_topic["label"]), str(old_topic["slug"]))
        if current_identity != old_identity:
            raise ValueError(
                f"topic reclassification {correction_id} old_topic does not match current topic"
            )
        original_topics.setdefault(
            event_id,
            json.loads(canonical_json(current)),
        )
        item["topic"] = {
            "label": str(new_topic["label"]),
            "slug": str(new_topic["slug"]),
            "method": "correction",
            "confidence": None,
        }
        item["topic_reclassification"] = {
            "reclassification_id": correction_id,
            "original_topic": original_topics[event_id],
        }
    return pending, cursor


def _resolve_feedback_events(
    rows: Sequence[dict[str, Any]], documents: Sequence[SourceDocument]
) -> list[dict[str, Any]]:
    by_content_id = {document.content_id for document in documents}
    by_episode_key: dict[str, set[str]] = {}
    for document in documents:
        key = str(document.metadata.get("key") or "").strip()
        if key:
            by_episode_key.setdefault(key, set()).add(document.content_id)

    resolved_rows: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        candidates: set[str] = set()
        delivery_content_id = str(item.get("delivery_content_id") or item.get("content_id") or "")
        if delivery_content_id in by_content_id:
            candidates.add(delivery_content_id)
        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        explicit = str(
            metadata.get("source_content_id") or item.get("source_content_id") or ""
        ).strip()
        if explicit in by_content_id:
            candidates.add(explicit)
        episode_key = str(metadata.get("episode_key") or "").strip()
        candidates.update(by_episode_key.get(episode_key, set()))
        if len(candidates) == 1 and item.get("delivery_mapping_status") == "confirmed":
            source_content_id = next(iter(candidates))
            item["source_content_id"] = source_content_id
            item["content_id"] = source_content_id
            item["mapping_status"] = "confirmed"
            item["confirmed"] = True
        else:
            item["source_content_id"] = None
            # Keep the authoritative legacy/delivery identity only in its
            # explicit field; unresolved feedback must not look like it points
            # at a current KnowledgeIndex content item.
            item["content_id"] = None
            item["mapping_status"] = "pending"
            item["confirmed"] = False
        resolved_rows.append(item)
    return resolved_rows


def collect_sources(paths: SourcePaths) -> SourceSnapshot:
    archive, archive_cursor = load_archive(paths.archive)
    database, database_cursor = load_chat_db(paths.chat_db)
    sent, sent_cursor = load_sent_ledger(paths.sent_ledger)
    media_rows, media_cursor = load_media_ledger(paths.media_ledger)
    podcast, podcast_cursor = load_podcast(paths.podcast_root, media_rows)
    feedback, feedback_cursor = load_feedback(
        paths.feedback_events,
        paths.feedback_reclassifications,
    )
    documents = [*archive, *database, *sent, *podcast]
    unique: dict[str, SourceDocument] = {}
    for document in documents:
        prior = unique.get(document.content_id)
        if prior is None:
            unique[document.content_id] = document
            continue
        if prior.content_hash != document.content_hash:
            raise ValueError(f"content authority conflict for {document.content_id}")
        if prior != document:
            raise ValueError(
                f"duplicate content provenance conflict for {document.content_id}"
            )
    resolved_feedback = _resolve_feedback_events(feedback, tuple(unique.values()))
    return SourceSnapshot(
        documents=tuple(unique.values()),
        cursors={
            "archive": archive_cursor,
            "chat_db": database_cursor,
            "sent_content_ledger": sent_cursor,
            "media_sent_ledger": media_cursor,
            "podcast": podcast_cursor,
            "feedback": feedback_cursor,
        },
        feedback_events=tuple(resolved_feedback),
    )
