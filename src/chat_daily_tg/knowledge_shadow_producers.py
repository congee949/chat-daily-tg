"""Authoritative, read-only producers for KnowledgeIndex shadow evidence.

The public functions in this module only derive event dictionaries.  They do
not append to the shadow journal, mutate a generation, or read any network
service.  Callers may pass their return values directly to
``knowledge_shadow.append_shadow_event``.
"""

from __future__ import annotations

import json
import re
import sqlite3
import stat
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from chat_daily_tg.knowledge_index import SourceDocument, sha256_text
from chat_daily_tg.knowledge_sources import SourceSnapshot


_GENERATION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")


class _CatalogAuditError(ValueError):
    """Stable, non-sensitive catalog error suitable for a shadow event."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _canonical_json(value: Any) -> str:
    """Return the builder-compatible encoding while rejecting non-finite data."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _canonical_clone(value: Any) -> Any:
    return json.loads(_canonical_json(value))


def _rows_hash(rows: Iterable[dict[str, Any]]) -> str:
    ordered = sorted((_canonical_clone(row) for row in rows), key=_canonical_json)
    return sha256_text(_canonical_json(ordered))


def _error_hash(code: str) -> str:
    return sha256_text(_canonical_json({"error": code}))


def _safe_rows_hash(rows: Iterable[dict[str, Any]], error_code: str) -> str:
    try:
        return _rows_hash(rows)
    except (TypeError, ValueError, OverflowError):
        return _error_hash(error_code)


def _safe_generation_catalog(generation_dir: Path) -> tuple[str, Path]:
    """Validate the caller-controlled path without resolving through symlinks."""

    directory = generation_dir.expanduser().absolute()
    generation_id = directory.name
    if (
        not generation_id
        or generation_id in {".", ".."}
        or _GENERATION_ID.fullmatch(generation_id) is None
    ):
        raise ValueError(f"invalid generation directory: {directory}")
    if directory.is_symlink() or directory.parent.is_symlink():
        raise ValueError(f"generation directory must not be a symlink: {directory}")
    if not directory.is_dir():
        raise ValueError(f"generation directory is missing: {directory}")

    catalog = directory / "catalog.sqlite"
    if catalog.is_symlink():
        raise ValueError(f"generation catalog must not be a symlink: {catalog}")
    if catalog.exists():
        info = catalog.stat(follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"generation catalog must be a regular file: {catalog}")
    return generation_id, catalog


def _open_catalog(catalog: Path) -> tuple[sqlite3.Connection, tuple[int, int]]:
    if not catalog.is_file():
        raise _CatalogAuditError("catalog_missing")
    before = catalog.stat(follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode):
        raise _CatalogAuditError("catalog_not_regular")
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(f"{catalog.as_uri()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
    except sqlite3.Error as exc:
        if connection is not None:
            connection.close()
        raise _CatalogAuditError("catalog_database_error") from exc
    return connection, (before.st_dev, before.st_ino)


def _require_catalog_unchanged(catalog: Path, identity: tuple[int, int]) -> None:
    if catalog.is_symlink() or not catalog.is_file():
        raise _CatalogAuditError("catalog_changed_during_audit")
    after = catalog.stat(follow_symlinks=False)
    if (after.st_dev, after.st_ino) != identity:
        raise _CatalogAuditError("catalog_changed_during_audit")


def _require_generation_identity(
    connection: sqlite3.Connection, generation_id: str
) -> None:
    try:
        rows = connection.execute(
            "SELECT generation_id FROM embedding_generations ORDER BY generation_id"
        ).fetchall()
    except sqlite3.Error as exc:
        raise _CatalogAuditError("catalog_generation_table_invalid") from exc
    if len(rows) != 1 or str(rows[0]["generation_id"]) != generation_id:
        raise _CatalogAuditError("catalog_generation_identity_mismatch")


def _required_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _CatalogAuditError(f"invalid_{label}")
    return value


def _integer(value: Any, label: str, *, nullable: bool = False) -> int | None:
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise _CatalogAuditError(f"invalid_{label}")
    return int(value)


def _boolean(value: Any, label: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and not isinstance(value, bool) and value in {0, 1}:
        return bool(value)
    raise _CatalogAuditError(f"invalid_{label}")


def _link_row(document: SourceDocument, link: Any) -> dict[str, Any]:
    return {
        "content_id": _required_text(document.content_id, "content_id"),
        "source_kind": _required_text(document.source_kind, "source_kind"),
        "source_ref": str(document.source_ref),
        "producer": str(document.producer),
        "authority": str(document.authority),
        "mapping_status": str(document.mapping_status),
        "content_active": _boolean(document.active, "content_active"),
        "ledger_schema": _required_text(link.ledger_schema, "ledger_schema"),
        "chat_id": _integer(link.chat_id, "chat_id"),
        "thread_id": _integer(link.thread_id, "thread_id", nullable=True),
        "message_id": _integer(link.message_id, "message_id"),
        "source_message_id": _integer(
            link.source_message_id, "source_message_id", nullable=True
        ),
        "confirmed": _boolean(link.confirmed, "confirmed"),
    }


def _expected_link_rows(snapshot: SourceSnapshot) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for document in snapshot.documents:
        for link in document.source_links:
            rows.append(_link_row(document, link))
    return rows


def _actual_link_rows(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    try:
        rows = connection.execute(
            "SELECT sl.content_id,ci.source_kind,ci.source_ref,ci.producer,"
            "ci.authority,ci.mapping_status,ci.active AS content_active,"
            "sl.ledger_schema,sl.chat_id,sl.thread_id,sl.message_id,"
            "sl.source_message_id,sl.confirmed "
            "FROM source_links AS sl "
            "LEFT JOIN content_items AS ci ON ci.content_id=sl.content_id "
            "ORDER BY sl.content_id,sl.ledger_schema,sl.chat_id,sl.message_id"
        ).fetchall()
    except sqlite3.Error as exc:
        raise _CatalogAuditError("catalog_source_link_schema_invalid") from exc
    output: list[dict[str, Any]] = []
    for row in rows:
        if row["source_kind"] is None:
            raise _CatalogAuditError("catalog_orphan_source_link")
        output.append(
            {
                "content_id": _required_text(row["content_id"], "content_id"),
                "source_kind": _required_text(row["source_kind"], "source_kind"),
                "source_ref": str(row["source_ref"]),
                "producer": str(row["producer"]),
                "authority": str(row["authority"]),
                "mapping_status": str(row["mapping_status"]),
                "content_active": _boolean(row["content_active"], "content_active"),
                "ledger_schema": _required_text(row["ledger_schema"], "ledger_schema"),
                "chat_id": _integer(row["chat_id"], "chat_id"),
                "thread_id": _integer(row["thread_id"], "thread_id", nullable=True),
                "message_id": _integer(row["message_id"], "message_id"),
                "source_message_id": _integer(
                    row["source_message_id"], "source_message_id", nullable=True
                ),
                "confirmed": _boolean(row["confirmed"], "confirmed"),
            }
        )
    return output


def _counter(rows: Iterable[dict[str, Any]]) -> Counter[str]:
    return Counter(_canonical_json(row) for row in rows)


def audit_source_links(
    generation_dir: Path, snapshot: SourceSnapshot
) -> dict[str, Any]:
    """Compare every authoritative source link with the candidate catalog.

    Provenance changes are represented as one missing and one extra row rather
    than being hidden behind the ledger's delivery-message uniqueness key.
    """

    generation_id, catalog = _safe_generation_catalog(generation_dir)
    try:
        expected = _expected_link_rows(snapshot)
        expected_hash = _rows_hash(expected)
    except (AttributeError, TypeError, ValueError, OverflowError) as exc:
        code = exc.code if isinstance(exc, _CatalogAuditError) else "source_snapshot_invalid"
        return {
            "kind": "source_link",
            "generation_id": generation_id,
            "accurate": False,
            "checked_count": 0,
            "expected_count": 0,
            "actual_count": 0,
            "missing": 0,
            "extra": 0,
            "expected_hash": _error_hash(code),
            "actual_hash": _error_hash("not_checked"),
            "error": code,
        }

    connection: sqlite3.Connection | None = None
    try:
        connection, identity = _open_catalog(catalog)
        _require_generation_identity(connection, generation_id)
        actual = _actual_link_rows(connection)
        _require_catalog_unchanged(catalog, identity)
    except (sqlite3.Error, _CatalogAuditError, OSError) as exc:
        code = exc.code if isinstance(exc, _CatalogAuditError) else "catalog_database_error"
        return {
            "kind": "source_link",
            "generation_id": generation_id,
            "accurate": False,
            "checked_count": len(expected),
            "expected_count": len(expected),
            "actual_count": 0,
            "missing": len(expected),
            "extra": 0,
            "expected_hash": expected_hash,
            "actual_hash": _error_hash(code),
            "error": code,
        }
    finally:
        if connection is not None:
            connection.close()

    expected_rows = _counter(expected)
    actual_rows = _counter(actual)
    missing = sum((expected_rows - actual_rows).values())
    extra = sum((actual_rows - expected_rows).values())
    checked_count = sum((expected_rows | actual_rows).values())
    accurate = checked_count > 0 and missing == 0 and extra == 0
    return {
        "kind": "source_link",
        "generation_id": generation_id,
        "accurate": accurate,
        "checked_count": checked_count,
        "expected_count": len(expected),
        "actual_count": len(actual),
        "missing": missing,
        "extra": extra,
        "expected_hash": expected_hash,
        "actual_hash": _rows_hash(actual),
        "error": None if accurate else ("source_links_empty" if checked_count == 0 else None),
    }


def _expected_cursor_rows(snapshot: SourceSnapshot) -> list[dict[str, Any]]:
    if not isinstance(snapshot.cursors, dict):
        raise _CatalogAuditError("source_cursors_invalid")
    rows: list[dict[str, Any]] = []
    for name, cursor in sorted(snapshot.cursors.items()):
        source_name = _required_text(name, "source_name")
        if not isinstance(cursor, dict):
            raise _CatalogAuditError("source_cursor_invalid")
        rows.append({"source_name": source_name, "cursor": _canonical_clone(cursor)})
    return rows


def _raw_cursor_rows(
    connection: sqlite3.Connection,
) -> list[dict[str, Any]]:
    try:
        rows = connection.execute(
            "SELECT source_name,cursor_json,last_event_hash,last_success,status "
            "FROM ingestion_cursors ORDER BY source_name"
        ).fetchall()
    except sqlite3.Error as exc:
        raise _CatalogAuditError("catalog_cursor_schema_invalid") from exc
    return [
        {
            "source_name": row["source_name"],
            "cursor_json": row["cursor_json"],
            "last_event_hash": row["last_event_hash"],
            "last_success": row["last_success"],
            "status": row["status"],
        }
        for row in rows
    ]


def _parse_cursor_rows(raw_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in raw_rows:
        source_name = _required_text(row["source_name"], "source_name")
        if source_name in seen:
            raise _CatalogAuditError("catalog_duplicate_cursor_source")
        seen.add(source_name)
        if not isinstance(row["cursor_json"], str):
            raise _CatalogAuditError("catalog_cursor_json_invalid")
        try:
            cursor = json.loads(row["cursor_json"])
        except json.JSONDecodeError as exc:
            raise _CatalogAuditError("catalog_cursor_json_invalid") from exc
        if not isinstance(cursor, dict):
            raise _CatalogAuditError("catalog_cursor_json_invalid")
        cursor = _canonical_clone(cursor)
        if row["last_event_hash"] != sha256_text(_canonical_json(cursor)):
            raise _CatalogAuditError("catalog_cursor_hash_mismatch")
        if not isinstance(row["last_success"], str) or not row["last_success"].strip():
            raise _CatalogAuditError("catalog_cursor_last_success_invalid")
        if row["status"] != "ok":
            raise _CatalogAuditError("catalog_cursor_status_invalid")
        output.append({"source_name": source_name, "cursor": cursor})
    return output


def audit_incremental_freshness(
    generation_dir: Path, snapshot: SourceSnapshot
) -> dict[str, Any]:
    """Compare source cursors without claiming that an index refresh occurred."""

    generation_id, catalog = _safe_generation_catalog(generation_dir)
    try:
        expected = _expected_cursor_rows(snapshot)
        expected_hash = _rows_hash(expected)
    except (AttributeError, TypeError, ValueError, OverflowError) as exc:
        code = exc.code if isinstance(exc, _CatalogAuditError) else "source_cursors_invalid"
        return {
            "kind": "source_freshness",
            "producer": "source-freshness.v1",
            "generation_id": generation_id,
            "success": False,
            "noop": False,
            "checked_count": 0,
            "expected_count": 0,
            "actual_count": 0,
            "missing": 0,
            "extra": 0,
            "changed_count": 0,
            "expected_hash": _error_hash(code),
            "actual_hash": _error_hash("not_checked"),
            "error": code,
        }

    connection: sqlite3.Connection | None = None
    raw_actual: list[dict[str, Any]] = []
    try:
        connection, identity = _open_catalog(catalog)
        _require_generation_identity(connection, generation_id)
        raw_actual = _raw_cursor_rows(connection)
        actual = _parse_cursor_rows(raw_actual)
        _require_catalog_unchanged(catalog, identity)
    except (sqlite3.Error, _CatalogAuditError, OSError) as exc:
        code = exc.code if isinstance(exc, _CatalogAuditError) else "catalog_database_error"
        actual_hash = (
            _safe_rows_hash(raw_actual, "catalog_cursor_rows_invalid")
            if raw_actual
            else _error_hash(code)
        )
        return {
            "kind": "source_freshness",
            "producer": "source-freshness.v1",
            "generation_id": generation_id,
            "success": False,
            "noop": False,
            "checked_count": len(expected),
            "expected_count": len(expected),
            "actual_count": len(raw_actual),
            "missing": len(expected),
            "extra": len(raw_actual),
            "changed_count": 0,
            "expected_hash": expected_hash,
            "actual_hash": actual_hash,
            "error": code,
        }
    finally:
        if connection is not None:
            connection.close()

    expected_by_name = {row["source_name"]: row["cursor"] for row in expected}
    actual_by_name = {row["source_name"]: row["cursor"] for row in actual}
    expected_names = set(expected_by_name)
    actual_names = set(actual_by_name)
    missing = len(expected_names - actual_names)
    extra = len(actual_names - expected_names)
    changed = sum(
        1
        for name in expected_names & actual_names
        if _canonical_json(expected_by_name[name]) != _canonical_json(actual_by_name[name])
    )
    expected_hash = _rows_hash(expected)
    actual_hash = _rows_hash(actual)
    noop = missing == 0 and extra == 0 and changed == 0 and expected_hash == actual_hash
    return {
        "kind": "source_freshness",
        "producer": "source-freshness.v1",
        "generation_id": generation_id,
        "success": True,
        "noop": noop,
        "checked_count": len(expected_names | actual_names),
        "expected_count": len(expected),
        "actual_count": len(actual),
        "missing": missing,
        "extra": extra,
        "changed_count": changed,
        "expected_hash": expected_hash,
        "actual_hash": actual_hash,
        "error": None,
    }


__all__ = ["audit_incremental_freshness", "audit_source_links"]
