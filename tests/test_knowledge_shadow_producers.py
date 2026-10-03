from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from chat_daily_tg.knowledge_index import SourceDocument, SourceLink, sha256_text
from chat_daily_tg.knowledge_shadow import validate_shadow_event
from chat_daily_tg.knowledge_shadow_producers import (
    audit_incremental_freshness,
    audit_source_links,
)
from chat_daily_tg.knowledge_sources import SourceSnapshot


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _snapshot(
    *,
    documents: tuple[SourceDocument, ...] | None = None,
    cursors: dict[str, dict] | None = None,
) -> SourceSnapshot:
    document = SourceDocument(
        content_id="content-a",
        source_kind="podcast",
        source_ref="episode-a",
        text="authoritative text",
        producer="Podcast4Bot",
        authority="Podcast4Bot",
        mapping_status="confirmed",
        source_links=[
            SourceLink(
                chat_id=10,
                thread_id=20,
                message_id=30,
                source_message_id=40,
                ledger_schema="media-sent.v2",
                confirmed=True,
            )
        ],
    )
    return SourceSnapshot(
        documents=documents if documents is not None else (document,),
        cursors=cursors if cursors is not None else {"podcast": {"mode": "set", "count": 1}},
        feedback_events=(),
    )


def _catalog(
    tmp_path: Path,
    *,
    generation_id: str = "gen-a",
    document: SourceDocument | None = None,
    cursor: dict | None = None,
) -> Path:
    generation = tmp_path / generation_id
    generation.mkdir(parents=True)
    path = generation / "catalog.sqlite"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE embedding_generations (generation_id TEXT PRIMARY KEY);
        CREATE TABLE content_items (
            content_id TEXT PRIMARY KEY,
            source_kind TEXT NOT NULL,
            source_ref TEXT NOT NULL,
            producer TEXT NOT NULL,
            authority TEXT NOT NULL,
            mapping_status TEXT NOT NULL,
            active INTEGER NOT NULL
        );
        CREATE TABLE source_links (
            content_id TEXT NOT NULL,
            chat_id INTEGER NOT NULL,
            thread_id INTEGER,
            message_id INTEGER NOT NULL,
            source_message_id INTEGER,
            ledger_schema TEXT NOT NULL,
            confirmed INTEGER NOT NULL
        );
        CREATE TABLE ingestion_cursors (
            source_name TEXT PRIMARY KEY,
            cursor_json TEXT NOT NULL,
            last_event_hash TEXT NOT NULL,
            last_success TEXT NOT NULL,
            status TEXT NOT NULL
        );
        """
    )
    connection.execute("INSERT INTO embedding_generations VALUES(?)", (generation_id,))
    source = document or _snapshot().documents[0]
    connection.execute(
        "INSERT INTO content_items VALUES(?,?,?,?,?,?,?)",
        (
            source.content_id,
            source.source_kind,
            source.source_ref,
            source.producer,
            source.authority,
            source.mapping_status,
            int(source.active),
        ),
    )
    for link in source.source_links:
        connection.execute(
            "INSERT INTO source_links VALUES(?,?,?,?,?,?,?)",
            (
                source.content_id,
                link.chat_id,
                link.thread_id,
                link.message_id,
                link.source_message_id,
                link.ledger_schema,
                int(link.confirmed),
            ),
        )
    value = cursor if cursor is not None else {"mode": "set", "count": 1}
    raw = _canonical(value)
    connection.execute(
        "INSERT INTO ingestion_cursors VALUES(?,?,?,?,?)",
        ("podcast", raw, sha256_text(raw), "2026-08-26T00:00:00+00:00", "ok"),
    )
    connection.commit()
    connection.close()
    return generation


def test_exact_source_links_and_cursors_produce_appendable_events(tmp_path) -> None:
    snapshot = _snapshot()
    generation = _catalog(tmp_path)

    links = audit_source_links(generation, snapshot)
    freshness = audit_incremental_freshness(generation, snapshot)

    assert links["accurate"] is True
    assert links["checked_count"] == links["expected_count"] == links["actual_count"] == 1
    assert links["missing"] == links["extra"] == 0
    assert links["expected_hash"] == links["actual_hash"]
    assert freshness["success"] is freshness["noop"] is True
    assert freshness["expected_hash"] == freshness["actual_hash"]
    assert validate_shadow_event(links)["accurate"] is True
    assert validate_shadow_event(freshness)["success"] is True


def test_source_link_audit_detects_missing_and_extra_rows(tmp_path) -> None:
    snapshot = _snapshot()
    extra = SourceDocument(
        content_id="content-extra",
        source_kind="podcast",
        source_ref="episode-extra",
        text="extra",
        producer="Podcast4Bot",
        authority="Podcast4Bot",
        mapping_status="confirmed",
        source_links=[
            SourceLink(chat_id=11, message_id=31, ledger_schema="media-sent.v2")
        ],
    )
    generation = _catalog(tmp_path, document=extra)

    event = audit_source_links(generation, snapshot)

    assert event["accurate"] is False
    assert event["expected_count"] == event["actual_count"] == 1
    assert event["missing"] == 1
    assert event["extra"] == 1
    assert event["expected_hash"] != event["actual_hash"]


def test_source_link_audit_detects_wrong_content_provenance(tmp_path) -> None:
    snapshot = _snapshot()
    source = snapshot.documents[0]
    wrong = SourceDocument(
        content_id=source.content_id,
        source_kind="telegram_archive",
        source_ref=source.source_ref,
        text=source.text,
        producer="wrong-producer",
        authority=source.authority,
        mapping_status=source.mapping_status,
        source_links=list(source.source_links),
    )
    generation = _catalog(tmp_path, document=wrong)

    event = audit_source_links(generation, snapshot)

    assert event["accurate"] is False
    assert event["missing"] == 1
    assert event["extra"] == 1


def test_incremental_freshness_marks_changed_cursor_not_success(tmp_path) -> None:
    generation = _catalog(tmp_path, cursor={"mode": "set", "count": 1})
    snapshot = _snapshot(cursors={"podcast": {"mode": "set", "count": 2}})

    event = audit_incremental_freshness(generation, snapshot)

    assert event["success"] is True
    assert event["noop"] is False
    assert event["changed_count"] == 1
    assert event["missing"] == event["extra"] == 0
    assert event["error"] is None
    assert event["expected_hash"] != event["actual_hash"]


def test_incremental_freshness_fails_closed_on_corrupt_catalog(tmp_path) -> None:
    generation = tmp_path / "gen-a"
    generation.mkdir()
    (generation / "catalog.sqlite").write_bytes(b"not a sqlite database")

    event = audit_incremental_freshness(generation, _snapshot())
    links = audit_source_links(generation, _snapshot())

    assert event["success"] is False
    assert event["noop"] is False
    assert event["error"] == "catalog_generation_table_invalid"
    assert len(event["expected_hash"]) == len(event["actual_hash"]) == 64
    assert links["accurate"] is False
    assert links["error"] == "catalog_generation_table_invalid"


def test_incremental_freshness_fails_closed_on_malformed_cursor_json(tmp_path) -> None:
    generation = _catalog(tmp_path)
    connection = sqlite3.connect(generation / "catalog.sqlite")
    connection.execute(
        "UPDATE ingestion_cursors SET cursor_json=? WHERE source_name=?",
        ("{not-json", "podcast"),
    )
    connection.commit()
    connection.close()

    event = audit_incremental_freshness(generation, _snapshot())

    assert event["success"] is False
    assert event["actual_count"] == 1
    assert event["error"] == "catalog_cursor_json_invalid"
    assert len(event["expected_hash"]) == len(event["actual_hash"]) == 64


def test_rejects_generation_and_catalog_symlinks(tmp_path) -> None:
    generation = _catalog(tmp_path / "real")
    linked_generation = tmp_path / "linked-generation"
    linked_generation.symlink_to(generation, target_is_directory=True)

    with pytest.raises(ValueError, match="must not be a symlink"):
        audit_source_links(linked_generation, _snapshot())

    real_generation = tmp_path / "catalog-link-generation"
    real_generation.mkdir()
    (real_generation / "catalog.sqlite").symlink_to(generation / "catalog.sqlite")
    with pytest.raises(ValueError, match="catalog must not be a symlink"):
        audit_incremental_freshness(real_generation, _snapshot())
