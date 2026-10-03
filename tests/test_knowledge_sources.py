from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

import chat_daily_tg.knowledge_sources as knowledge_sources
from chat_daily_tg.knowledge_index import (
    AssetRecord,
    SourceDocument,
    SourceLink,
    canonical_json,
    parse_archive_messages,
    sha256_text,
)
from chat_daily_tg.knowledge_sources import (
    SourcePaths,
    collect_sources,
    load_archive,
    load_chat_db,
    load_feedback,
    load_media_ledger,
    load_podcast,
    load_sent_ledger,
)


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def _sent_row(*, content_id: str, message_id: int, content: str = "card text") -> dict:
    return {
        "schema": "sent-content.v1",
        "chat_id": -100123,
        "thread_id": 9,
        "message_id": message_id,
        "producer": "chatdaily_raw",
        "source_kind": "telegram_channel",
        "source_ref": "https://t.me/public_channel/101",
        "source_message_ids": [101],
        "url": f"https://example.com/{content_id}",
        "content": content,
        "content_hash": sha256_text(content),
        "sent_at": "2026-08-25T10:00:00+08:00",
        "delivery_state": "confirmed",
        "content_id": content_id,
    }


def _feedback_event(event_id: str, content_id: str, episode_key: str) -> dict:
    mapping = {
        "mapping_status": "confirmed",
        "chat_id": -100123,
        "thread_id": 9,
        "message_id": 77,
        "kind": "telegram",
    }
    return {
        "schema": "intent-feedback.v1",
        "event_id": event_id,
        "idempotency_key": event_id,
        "event_type": "read",
        "intent": "read",
        "content_id": content_id,
        "topic": {
            "label": "金融与市场",
            "slug": "金融与市场",
            "method": "explicit",
            "confidence": 1.0,
        },
        "source": dict(mapping),
        "target": dict(mapping),
        "metadata": {"episode_key": episode_key},
    }


def _podcast_meta(path: Path, *, key: str, url: str, platform: str = "youtube", **extra) -> None:
    payload = {
        "key": key,
        "url": url,
        "platform": platform,
        "title": "Synthetic title",
        **extra,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _patch_collect_loaders(
    monkeypatch: pytest.MonkeyPatch,
    *,
    sent: list[SourceDocument],
    podcast: list[SourceDocument],
) -> None:
    monkeypatch.setattr(knowledge_sources, "load_archive", lambda _path: ([], {}))
    monkeypatch.setattr(knowledge_sources, "load_chat_db", lambda _path: ([], {}))
    monkeypatch.setattr(
        knowledge_sources, "load_sent_ledger", lambda _path: (sent, {})
    )
    monkeypatch.setattr(
        knowledge_sources, "load_media_ledger", lambda _path: ([], {})
    )
    monkeypatch.setattr(
        knowledge_sources, "load_podcast", lambda _path, _rows: (podcast, {})
    )
    monkeypatch.setattr(
        knowledge_sources, "load_feedback", lambda _events, _reclassifications: ([], {})
    )


def _empty_source_paths(tmp_path: Path) -> SourcePaths:
    return SourcePaths(
        archive=tmp_path / "archive",
        chat_db=tmp_path / "chat-daily.db",
        sent_ledger=tmp_path / "sent.jsonl",
        media_ledger=tmp_path / "media.jsonl",
        podcast_root=tmp_path / "Podcast4Bot",
        feedback_events=tmp_path / "feedback.jsonl",
        feedback_reclassifications=tmp_path / "reclassifications.jsonl",
    )


def test_archive_uses_allowlist_and_marks_partial_parser_raw(tmp_path: Path) -> None:
    day = tmp_path / "2026" / "08" / "25"
    day.mkdir(parents=True)
    (day / "wechat-complete.md").write_text(
        "# WeChat\n\n### 2026-08-25 09:00\n\n**Alice**: hello\n",
        encoding="utf-8",
    )
    (day / "wechat-partial.md").write_text(
        "# WeChat\n\n### 2026-08-25 09:00\n\nnot-a-sender\n"
        "### 2026-08-25 09:01\n\n**Bob**: retained\n",
        encoding="utf-8",
    )
    (day / "telegram-public.md").write_text(
        "# Telegram\n\n[Telegram / Public / 09:00 / Alice] hello\n",
        encoding="utf-8",
    )
    (day / "summary.md").write_text("# Summary\n\nsummary", encoding="utf-8")
    concise = day / "concise.md"
    concise.write_text("# Concise\n\nconcise", encoding="utf-8")
    (day / "health-briefing.md").write_text("# Derived\n\nignore", encoding="utf-8")
    (day / "arbitrary-derived.md").write_text("# Derived\n\nignore", encoding="utf-8")

    documents, cursor = load_archive(tmp_path)

    assert len(documents) == 5
    assert cursor["count"] == 5
    assert {document.source_ref for document in documents} == {
        "2026/08/25/wechat-complete.md",
        "2026/08/25/wechat-partial.md",
        "2026/08/25/telegram-public.md",
        "2026/08/25/summary.md",
        "2026/08/25/concise.md",
    }
    by_name = {Path(document.source_ref).name: document for document in documents}
    assert by_name["wechat-complete.md"].source_kind == "wechat_archive"
    assert by_name["wechat-complete.md"].metadata["parser_coverage"] == 1.0
    assert by_name["wechat-partial.md"].source_kind == "wechat_archive_raw"
    assert by_name["wechat-partial.md"].metadata["parser_coverage"] == 0.5
    assert by_name["wechat-partial.md"].content_id == "archive:2026/08/25/wechat-partial.md"
    assert by_name["wechat-partial.md"].metadata["identity_scope"] == "file"
    assert by_name["wechat-partial.md"].metadata["identity_reason"].startswith("raw_fallback:")
    assert "member_ids" not in by_name["wechat-partial.md"].metadata
    assert by_name["telegram-public.md"].source_kind == "telegram_archive"
    assert by_name["telegram-public.md"].metadata["parser_mode"] == "structured"
    assert by_name["telegram-public.md"].metadata["parser_coverage"] == 1.0
    assert by_name["telegram-public.md"].metadata["identity_scope"] == "message_session"
    assert by_name["telegram-public.md"].published_at == "2026-08-25"
    assert by_name["summary.md"].content_id == "archive:2026/08/25/summary.md"
    assert by_name["summary.md"].metadata["identity_scope"] == "file"
    assert by_name["summary.md"].metadata["identity_reason"] == "summary_file_level_by_design"

    before = cursor
    stat = concise.stat()
    os.utime(concise, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    _documents, after = load_archive(tmp_path)
    assert after == before
    assert "mtime_ns" not in json.dumps(after)


def test_archive_rejects_invalid_utf8(tmp_path: Path) -> None:
    day = tmp_path / "2026" / "08" / "25"
    day.mkdir(parents=True)
    (day / "wechat-bad.md").write_bytes(b"# bad\n\xff")

    with pytest.raises(UnicodeDecodeError):
        load_archive(tmp_path)


def test_partial_telegram_archive_uses_whole_file_raw_fallback(tmp_path: Path) -> None:
    day = tmp_path / "2026" / "08" / "25"
    day.mkdir(parents=True)
    (day / "telegram-partial.md").write_text(
        "# Telegram\n\n"
        "[Telegram / Public / 09:00 / Alice] retained\n\n"
        "[Telegram / Public / 09:01 / Bob]\n",
        encoding="utf-8",
    )

    documents, _cursor = load_archive(tmp_path)

    assert len(documents) == 1
    assert documents[0].source_kind == "telegram_archive_raw"
    assert documents[0].metadata["message_blocks"] == 2
    assert documents[0].metadata["parsed_message_blocks"] == 1
    assert documents[0].metadata["parser_coverage"] == 0.5
    assert documents[0].content_id == "archive:2026/08/25/telegram-partial.md"
    assert documents[0].metadata["identity_scope"] == "file"
    assert "member_ids" not in documents[0].metadata


def test_archive_sessions_split_only_after_strict_ten_minute_gap_and_keep_cursor_ids(
    tmp_path: Path,
) -> None:
    day = tmp_path / "2026" / "08" / "25"
    day.mkdir(parents=True)
    (day / "telegram-public.md").write_text(
        "# Telegram\n\n"
        "[Telegram / Public / 09:00 / Alice] first\n\n"
        "[Telegram / Public / 09:10 / Bob] exactly ten\n\n"
        "[Telegram / Public / 09:20 / Carol] exactly ten again\n\n"
        "[Telegram / Public / 09:31 / Dan] more than ten\n",
        encoding="utf-8",
    )

    documents, cursor = load_archive(tmp_path)

    assert len(documents) == 2
    assert [document.metadata["member_count"] for document in documents] == [3, 1]
    assert [document.metadata["session_index"] for document in documents] == [1, 2]
    assert all(document.metadata["session_count"] == 2 for document in documents)
    assert "exactly ten again" in documents[0].text
    assert "more than ten" not in documents[0].text
    assert "more than ten" in documents[1].text
    assert all(document.content_id.startswith("archive-session:v1:") for document in documents)
    assert cursor["count"] == 1
    assert cursor["document_ids"] == sorted(document.content_id for document in documents)

    for document in documents:
        reparsed = parse_archive_messages(
            source_kind=document.source_kind,
            source_ref=document.source_ref,
            text=document.text,
            published_at=document.published_at,
        )
        assert [message.member_id for message in reparsed] == document.metadata["member_ids"]


def test_wechat_session_id_is_stable_but_order_sensitive(tmp_path: Path) -> None:
    day = tmp_path / "2026" / "08" / "25"
    day.mkdir(parents=True)
    archive = day / "wechat-public.md"
    first = "### 2026-08-25 09:00\n\n**Alice**: first"
    second = "### 2026-08-25 09:01\n\n**Bob**: second"
    archive.write_text(f"# Original title\n\n{first}\n\n{second}\n", encoding="utf-8")

    original_documents, _cursor = load_archive(tmp_path)
    assert len(original_documents) == 1
    original = original_documents[0]
    member_ids = original.metadata["member_ids"]
    expected_digest = sha256_text(
        canonical_json(
            {
                "source_kind": "wechat_archive",
                "source_ref": "2026/08/25/wechat-public.md",
                "member_ids": member_ids,
            }
        )
    )
    assert original.content_id == f"archive-session:v1:{expected_digest}"

    archive.write_text(f"# Changed title\n\n{first}\n\n{second}\n", encoding="utf-8")
    title_changed_documents, _cursor = load_archive(tmp_path)
    assert title_changed_documents[0].content_id == original.content_id

    archive.write_text(f"# Changed title\n\n{second}\n\n{first}\n", encoding="utf-8")
    reordered_documents, reordered_cursor = load_archive(tmp_path)
    assert len(reordered_documents) == 1
    reordered = reordered_documents[0]
    assert reordered.metadata["member_ids"] == list(reversed(member_ids))
    assert reordered.content_id != original.content_id
    assert reordered_cursor["document_ids"] == [reordered.content_id]


def test_legacy_archive_file_feedback_stays_pending_without_guessed_session_mapping(
    tmp_path: Path,
) -> None:
    paths = _empty_source_paths(tmp_path)
    day = paths.archive / "2026" / "08" / "25"
    day.mkdir(parents=True)
    relative = "2026/08/25/telegram-public.md"
    (day / "telegram-public.md").write_text(
        "# Telegram\n\n[Telegram / Public / 09:00 / Alice] retained\n",
        encoding="utf-8",
    )
    legacy_content_id = f"archive:{relative}"
    _write_jsonl(
        paths.feedback_events,
        [_feedback_event("legacy-archive-feedback", legacy_content_id, "")],
    )

    snapshot = collect_sources(paths)

    assert len(snapshot.documents) == 1
    assert snapshot.documents[0].content_id.startswith("archive-session:v1:")
    event = snapshot.feedback_events[0]
    assert event["delivery_content_id"] == legacy_content_id
    assert event["content_id"] is None
    assert event["source_content_id"] is None
    assert event["mapping_status"] == "pending"
    assert event["confirmed"] is False


def test_zero_message_or_derived_telegram_files_are_cursor_only(tmp_path: Path) -> None:
    day = tmp_path / "2026" / "08" / "25"
    day.mkdir(parents=True)
    (day / "telegram-empty.md").write_text(
        "# Telegram: Empty\n\n> 导出 0 条消息\n\n> 跳过空文本/低信息消息 0 条\n",
        encoding="utf-8",
    )
    (day / "telegram-backfill-summary.md").write_text(
        "# Telegram backfill summary\n\n- derived observation\n",
        encoding="utf-8",
    )

    documents, cursor = load_archive(tmp_path)

    assert documents == []
    assert cursor["count"] == 2


def test_sent_ledger_groups_only_same_native_content_id(tmp_path: Path) -> None:
    path = tmp_path / "sent.jsonl"
    first = _sent_row(content_id="telegram-channel:1:101", message_id=10)
    second_delivery = dict(first, message_id=11)
    other = _sent_row(content_id="telegram-channel:2:202", message_id=12, content="other card")
    other["source_ref"] = "https://t.me/other_channel/202"
    other["source_message_ids"] = [202]
    other["sent_at"] = "2026-08-25T10:01:00+08:00"
    _write_jsonl(path, [first, second_delivery, other])

    documents, cursor = load_sent_ledger(path)

    assert [document.content_id for document in documents] == [
        "telegram-channel:1:101",
        "telegram-channel:2:202",
    ]
    assert len(documents[0].source_links) == 2
    assert documents[0].metadata["delivery_count"] == 2
    assert documents[0].representation_type == "ledger_content"
    assert cursor["count"] == 3
    assert cursor["schema"] == "sent-content.v1"
    assert len(cursor["event_hashes"]) == 3
    assert cursor["document_ids"] == [
        "telegram-channel:1:101",
        "telegram-channel:2:202",
    ]
    assert cursor["delivery_ids"] == ["-100123:10", "-100123:11", "-100123:12"]


def test_sent_ledger_preserves_every_source_message_id_in_bundle(tmp_path: Path) -> None:
    path = tmp_path / "sent.jsonl"
    row = _sent_row(content_id="telegram-channel:1:101", message_id=10)
    row["source_message_ids"] = [101, 102, 103]
    _write_jsonl(path, [row])

    documents, _cursor = load_sent_ledger(path)

    assert documents[0].metadata["source_message_ids"] == [101, 102, 103]
    assert documents[0].source_links[0].source_message_id == 101


def test_sent_ledger_fails_closed_on_delivery_or_content_conflict(tmp_path: Path) -> None:
    path = tmp_path / "sent.jsonl"
    first = _sent_row(content_id="telegram-channel:1:101", message_id=10)
    target_conflict = _sent_row(content_id="telegram-channel:2:202", message_id=10)
    _write_jsonl(path, [first, target_conflict])
    with pytest.raises(ValueError, match="delivery"):
        load_sent_ledger(path)

    content_conflict = dict(first, message_id=11, content="changed")
    content_conflict["content_hash"] = sha256_text("changed")
    _write_jsonl(path, [first, content_conflict])
    with pytest.raises(ValueError, match="content"):
        load_sent_ledger(path)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda row: row.update(thread_id=10),
        lambda row: row.update(source_message_ids=[102]),
        lambda row: row.update(delivery_state="pending"),
    ],
    ids=["thread-id", "source-message-id", "confirmed"],
)
def test_sent_ledger_rejects_duplicate_delivery_source_link_conflicts(
    tmp_path: Path, mutation
) -> None:
    path = tmp_path / "sent.jsonl"
    first = _sent_row(content_id="telegram-channel:1:101", message_id=10)
    conflicting = dict(first)
    mutation(conflicting)
    _write_jsonl(path, [first, conflicting])

    with pytest.raises(ValueError, match="authority conflict for delivery link"):
        load_sent_ledger(path)


def test_sent_ledger_rejects_duplicate_delivery_schema_conflict(tmp_path: Path) -> None:
    path = tmp_path / "sent.jsonl"
    first = _sent_row(content_id="telegram-channel:1:101", message_id=10)
    conflicting = dict(first, schema="sent-content.v2")
    _write_jsonl(path, [first, conflicting])

    with pytest.raises(ValueError, match="unexpected ledger schema"):
        load_sent_ledger(path)


def test_sent_ledger_accepts_identical_duplicate_delivery_idempotently(tmp_path: Path) -> None:
    path = tmp_path / "sent.jsonl"
    row = _sent_row(content_id="telegram-channel:1:101", message_id=10)
    _write_jsonl(path, [row, dict(row)])

    documents, cursor = load_sent_ledger(path)

    assert len(documents) == 1
    assert len(documents[0].source_links) == 1
    assert documents[0].metadata["delivery_count"] == 1
    assert cursor["count"] == 2
    assert cursor["delivery_ids"] == ["-100123:10"]


def test_media_and_podcast_require_url_id_producer_agreement(tmp_path: Path) -> None:
    ledger_path = tmp_path / "media.jsonl"
    url = "https://www.youtube.com/watch?v=abcdefghijk"
    row = {
        "chat_id": -100123,
        "thread_id": 9,
        "message_id": 55,
        "id": "youtube:abcdefghijk",
        "url": url,
        "producer": "youtube",
        "ts": "2026-08-25T10:00:00+08:00",
    }
    _write_jsonl(ledger_path, [row])
    media_rows, _cursor = load_media_ledger(ledger_path)
    root = tmp_path / "Podcast4Bot"
    _podcast_meta(root / "transcripts" / "abcd1234.meta.json", key="abcd1234", url=url)
    (root / "transcripts" / "abcd1234.srt").write_text(
        "1\n00:00:00,000 --> 00:00:01,000\nhello\n",
        encoding="utf-8",
    )

    documents, _cursor = load_podcast(root, media_rows)

    assert len(documents) == 1
    assert documents[0].content_id == "youtube:abcdefghijk"
    assert documents[0].mapping_status == "confirmed"
    assert len(documents[0].source_links) == 1

    _podcast_meta(
        root / "transcripts" / "abcd1234.meta.json",
        key="abcd1234",
        url=url,
        platform="bilibili",
    )
    with pytest.raises(ValueError, match="producer"):
        load_podcast(root, media_rows)


def test_media_ledger_rejects_url_or_content_authority_conflicts(tmp_path: Path) -> None:
    path = tmp_path / "media.jsonl"
    base = {
        "chat_id": -100123,
        "thread_id": 9,
        "message_id": 1,
        "id": "youtube:one",
        "url": "https://www.youtube.com/watch?v=oneone1",
        "producer": "youtube",
        "ts": "2026-08-25T10:00:00+08:00",
    }
    conflict = dict(base, message_id=2, id="youtube:two")
    _write_jsonl(path, [base, conflict])

    with pytest.raises(ValueError, match="URL"):
        load_media_ledger(path)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda row: row.update(thread_id=10),
        lambda row: row.update(schema="media-sent.v1"),
        lambda row: row.update(ts="2026-08-25T10:00:01+08:00"),
        lambda row: row.update(id="youtube:different"),
        lambda row: row.update(url="https://www.youtube.com/watch?v=different"),
        lambda row: row.update(producer="bilibili"),
    ],
    ids=["thread-id", "schema", "timestamp", "content-id", "url", "producer"],
)
def test_media_ledger_rejects_duplicate_delivery_provenance_conflicts(
    tmp_path: Path, mutation
) -> None:
    path = tmp_path / "media.jsonl"
    first = {
        "chat_id": -100123,
        "thread_id": 9,
        "message_id": 55,
        "id": "youtube:abcdefghijk",
        "url": "https://www.youtube.com/watch?v=abcdefghijk",
        "producer": "youtube",
        "ts": "2026-08-25T10:00:00+08:00",
    }
    conflicting = dict(first)
    mutation(conflicting)
    _write_jsonl(path, [first, conflicting])

    with pytest.raises(ValueError, match="authority conflict for delivery"):
        load_media_ledger(path)


def test_media_ledger_accepts_identical_duplicate_delivery_idempotently(tmp_path: Path) -> None:
    path = tmp_path / "media.jsonl"
    row = {
        "chat_id": -100123,
        "thread_id": 9,
        "message_id": 55,
        "id": "youtube:abcdefghijk",
        "url": "https://www.youtube.com/watch?v=abcdefghijk",
        "producer": "youtube",
        "ts": "2026-08-25T10:00:00+08:00",
    }
    _write_jsonl(path, [row, dict(row)])

    rows, cursor = load_media_ledger(path)

    assert rows == [row]
    assert cursor["count"] == 2
    assert cursor["delivery_ids"] == ["-100123:55"]
    assert cursor["content_ids"] == ["youtube:abcdefghijk"]


def test_media_ledger_strict_legacy_schema_and_inventory(tmp_path: Path) -> None:
    path = tmp_path / "media.jsonl"
    row = {
        "chat_id": -100123,
        "thread_id": 9,
        "message_id": 55,
        "id": "youtube:abcdefghijk",
        "url": "https://www.youtube.com/watch?v=abcdefghijk",
        "producer": "youtube",
        "ts": "2026-08-25T10:00:00+08:00",
    }
    _write_jsonl(path, [row])

    rows, cursor = load_media_ledger(path)

    assert rows == [row]
    assert cursor["schema"] == "media-sent.legacy-v1"
    assert cursor["event_hashes"]
    assert cursor["delivery_ids"] == ["-100123:55"]
    assert cursor["content_ids"] == ["youtube:abcdefghijk"]


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda row: row.pop("ts"), "missing fields"),
        (lambda row: row.pop("id"), "missing fields"),
        (lambda row: row.update(extra="unexpected"), "unknown fields"),
        (lambda row: row.update(ts="not-a-timestamp"), "ISO-8601"),
        (lambda row: row.update(ts="2026-08-25T10:00:00"), "timezone"),
        (lambda row: row.update(schema="media-sent.v2"), "unsupported"),
        (lambda row: row.update(message_id="55"), "must be an integer"),
    ],
)
def test_media_ledger_rejects_schema_shape_and_timestamp_changes(
    tmp_path: Path, mutation, message: str
) -> None:
    path = tmp_path / "media.jsonl"
    row = {
        "chat_id": -100123,
        "thread_id": 9,
        "message_id": 55,
        "id": "youtube:abcdefghijk",
        "url": "https://www.youtube.com/watch?v=abcdefghijk",
        "producer": "youtube",
        "ts": "2026-08-25T10:00:00+08:00",
    }
    mutation(row)
    _write_jsonl(path, [row])

    with pytest.raises(ValueError, match=message):
        load_media_ledger(path)


def test_media_ledger_rejects_mixed_schema_rows(tmp_path: Path) -> None:
    path = tmp_path / "media.jsonl"
    first = {
        "chat_id": -100123,
        "message_id": 55,
        "id": "youtube:abcdefghijk",
        "url": "https://www.youtube.com/watch?v=abcdefghijk",
        "producer": "youtube",
        "ts": "2026-08-25T10:00:00+08:00",
    }
    second = {
        **first,
        "schema": "media-sent.v1",
        "message_id": 56,
    }
    _write_jsonl(path, [first, second])

    with pytest.raises(ValueError, match="mixes incompatible schemas"):
        load_media_ledger(path)


def test_bilibili_article_uses_explicit_subscription_producer_adapter(tmp_path: Path) -> None:
    url = "https://www.bilibili.com/read/cv12345"
    media_rows = [
        {
            "chat_id": -100123,
            "thread_id": 9,
            "message_id": 3,
            "id": "bilibili:article:12345",
            "url": url,
            "producer": "bilibili",
        }
    ]
    root = tmp_path / "Podcast4Bot"
    _podcast_meta(
        root / "articles" / "abcd1234.meta.json",
        key="abcd1234",
        url=url,
        platform="bilibili_article",
        kind="article",
    )
    (root / "articles" / "abcd1234.txt").write_text("article body", encoding="utf-8")

    documents, _cursor = load_podcast(root, media_rows)

    assert documents[0].content_id == "bilibili:article:12345"
    assert documents[0].mapping_status == "confirmed"
    assert documents[0].producer == "bilibili_article"


def test_podcast_nonmonotonic_srt_falls_back_to_strict_txt(tmp_path: Path) -> None:
    root = tmp_path / "Podcast4Bot"
    folder = root / "transcripts"
    _podcast_meta(
        folder / "abcd1234.meta.json",
        key="abcd1234",
        url="https://www.youtube.com/watch?v=abcdefghijk",
    )
    (folder / "abcd1234.srt").write_text(
        "1\n00:00:05,000 --> 00:00:06,000\nlater\n\n2\n00:00:01,000 --> 00:00:02,000\nearlier\n",
        encoding="utf-8",
    )
    (folder / "abcd1234.txt").write_text("clean transcript", encoding="utf-8")

    documents, cursor = load_podcast(root, [])

    assert len(documents) == 1
    document = documents[0]
    assert document.text == "clean transcript"
    assert document.representation_type == "transcript"
    assert document.document_role == "original"
    assert document.metadata["srt_fallback_reason"] == "nonmonotonic_or_invalid_srt"
    assert document.content_id.startswith("podcast:v1:")
    assert cursor["count"] == 3  # meta + examined SRT + selected TXT


def test_podcast_metadata_only_is_not_original_transcript(tmp_path: Path) -> None:
    root = tmp_path / "Podcast4Bot"
    folder = root / "transcripts"
    _podcast_meta(
        folder / "withdesc.meta.json",
        key="withdesc",
        url="https://example.com/with-description",
        description="metadata description",
    )
    _podcast_meta(
        folder / "empty000.meta.json",
        key="empty000",
        url="https://example.com/empty",
        description="",
    )

    documents, _cursor = load_podcast(root, [])

    assert len(documents) == 1
    assert documents[0].representation_type == "metadata_description"
    assert documents[0].document_role == "metadata"
    assert documents[0].mapping_status == "source_only"


def test_podcast_preserves_multiple_representations_for_one_content(tmp_path: Path) -> None:
    root = tmp_path / "Podcast4Bot"
    url = "https://example.com/shared"
    _podcast_meta(
        root / "articles" / "shared.meta.json",
        key="shared",
        url=url,
        media_modality="gallery",
    )
    (root / "articles" / "shared.txt").write_text(
        "caption and OCR facts", encoding="utf-8"
    )
    _podcast_meta(
        root / "transcripts" / "shared.meta.json",
        key="shared",
        url=url,
        description="episode metadata description",
    )

    documents, cursor = load_podcast(root, [])

    assert len(documents) == 1
    document = documents[0]
    representations = {
        document.representation_type,
        *(value.representation_type for value in document.alternate_representations),
    }
    assert representations == {"vision_text", "metadata_description"}
    assert len(document.metadata["representations"]) == 2
    assert cursor["document_ids"] == [document.content_id]


def test_feedback_stays_pending_until_source_document_resolves(tmp_path: Path) -> None:
    feedback_path = tmp_path / "feedback" / "events.jsonl"
    unresolved = _feedback_event("event-unresolved", "sha256:" + "1" * 64, "missing")
    _write_jsonl(feedback_path, [unresolved])

    rows, _cursor = load_feedback(feedback_path)

    assert rows[0]["delivery_mapping_status"] == "confirmed"
    assert rows[0]["mapping_status"] == "pending"
    assert rows[0]["confirmed"] is False
    assert rows[0]["delivery_content_id"] == unresolved["content_id"]

    podcast_root = tmp_path / "Podcast4Bot"
    _podcast_meta(
        podcast_root / "transcripts" / "knownkey.meta.json",
        key="knownkey",
        url="https://example.com/known",
    )
    (podcast_root / "transcripts" / "knownkey.txt").write_text("source body", encoding="utf-8")
    resolved = _feedback_event("event-resolved", "sha256:" + "2" * 64, "knownkey")
    _write_jsonl(feedback_path, [resolved, unresolved])
    snapshot = collect_sources(
        SourcePaths(
            archive=tmp_path / "missing-archive",
            chat_db=tmp_path / "missing.db",
            sent_ledger=tmp_path / "missing-sent.jsonl",
            media_ledger=tmp_path / "missing-media.jsonl",
            podcast_root=podcast_root,
            feedback_events=feedback_path,
            feedback_reclassifications=(tmp_path / "feedback" / "topic_reclassifications.jsonl"),
        )
    )
    by_event = {event["event_id"]: event for event in snapshot.feedback_events}
    assert by_event["event-resolved"]["mapping_status"] == "confirmed"
    assert by_event["event-resolved"]["confirmed"] is True
    assert by_event["event-resolved"]["delivery_content_id"] == resolved["content_id"]
    assert by_event["event-resolved"]["content_id"].startswith("podcast:v1:")
    assert by_event["event-unresolved"]["mapping_status"] == "pending"


def test_collect_sources_rejects_same_text_with_conflicting_provenance(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    sent = SourceDocument(
        content_id="shared-content",
        source_kind="sent_content",
        source_ref="https://t.me/channel/101",
        text="same canonical body",
        authority="sent_ledger",
        mapping_status="confirmed",
        metadata={"source_message_ids": [101]},
        source_links=[
            SourceLink(
                chat_id=-100123,
                thread_id=9,
                message_id=501,
                source_message_id=101,
                ledger_schema="sent-content.v1",
            )
        ],
    )
    podcast = SourceDocument(
        content_id="shared-content",
        source_kind="youtube",
        source_ref="https://youtube.com/watch?v=shared",
        text="same canonical body",
        authority="producer_artifact",
        metadata={"key": "shared-episode"},
        assets=[AssetRecord(asset_id="shared-cover", sha256="a" * 64)],
    )
    assert sent.content_hash == podcast.content_hash
    _patch_collect_loaders(monkeypatch, sent=[sent], podcast=[podcast])

    with pytest.raises(ValueError, match="duplicate content provenance conflict"):
        collect_sources(_empty_source_paths(tmp_path))


def test_collect_sources_collapses_only_fully_equivalent_duplicates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    first = SourceDocument(
        content_id="shared-content",
        source_kind="sent_content",
        source_ref="https://t.me/channel/101",
        text="same canonical body",
        authority="sent_ledger",
        mapping_status="confirmed",
        metadata={"source_message_ids": [101]},
        source_links=[
            SourceLink(
                chat_id=-100123,
                thread_id=9,
                message_id=501,
                source_message_id=101,
                ledger_schema="sent-content.v1",
            )
        ],
    )
    equivalent = SourceDocument(
        content_id=first.content_id,
        source_kind=first.source_kind,
        source_ref=first.source_ref,
        text=first.text,
        authority=first.authority,
        mapping_status=first.mapping_status,
        metadata={"source_message_ids": [101]},
        source_links=list(first.source_links),
    )
    _patch_collect_loaders(monkeypatch, sent=[first], podcast=[equivalent])

    snapshot = collect_sources(_empty_source_paths(tmp_path))

    assert snapshot.documents == (first,)


def test_chat_db_cursor_hashes_wal_visible_rows_not_file_mtime(tmp_path: Path) -> None:
    path = tmp_path / "chat-daily.db"
    writer = sqlite3.connect(path)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute(
        "CREATE TABLE permanent ("
        "id TEXT PRIMARY KEY, title TEXT, content TEXT, notes TEXT, captured_at TEXT, "
        "source_group TEXT, source_sender TEXT, status TEXT, url TEXT)"
    )
    writer.execute(
        "INSERT INTO permanent VALUES(?,?,?,?,?,?,?,?,?)",
        ("p1", "title", "before", None, "2026-08-25", "group", "sender", "alive", None),
    )
    writer.commit()

    before_documents, before_cursor = load_chat_db(path)
    writer.execute("UPDATE permanent SET content='after' WHERE id='p1'")
    writer.commit()
    after_documents, after_cursor = load_chat_db(path)
    writer.close()

    assert before_documents[0].text != after_documents[0].text
    assert before_cursor["set_hash"] != after_cursor["set_hash"]
    assert "mtime_ns" not in after_cursor
    assert "sha256" not in after_cursor


def _topic_correction(
    correction_id: str,
    event_id: str,
    old_label: str,
    new_label: str,
) -> dict:
    return {
        "schema": "intent-feedback.topic-reclassification.v1",
        "reclassification_id": correction_id,
        "event_id": event_id,
        "occurred_at": "2026-08-25T10:00:00+08:00",
        "old_topic": {"label": old_label, "slug": old_label},
        "new_topic": {"label": new_label, "slug": new_label},
        "reason": "manual correction",
    }


def test_source_defaults_keep_feedback_facts_and_corrections_separate(tmp_path: Path) -> None:
    paths = SourcePaths.defaults(tmp_path)

    assert paths.feedback_events == tmp_path / "intent-feedback" / "events.jsonl"
    assert paths.feedback_reclassifications == (
        tmp_path / "intent-feedback" / "topic_reclassifications.jsonl"
    )


def test_feedback_applies_topic_reclassification_as_projection(tmp_path: Path) -> None:
    events = tmp_path / "intent-feedback" / "events.jsonl"
    corrections = tmp_path / "intent-feedback" / "topic_reclassifications.jsonl"
    event = _feedback_event("event-1", "sha256:" + "1" * 64, "episode")
    _write_jsonl(events, [event])
    _write_jsonl(corrections, [_topic_correction("topic-1", "event-1", "金融与市场", "科技与产品")])

    rows, cursor = load_feedback(events, corrections)

    assert len(rows) == 1
    assert rows[0]["topic"] == {
        "label": "科技与产品",
        "slug": "科技与产品",
        "method": "correction",
        "confidence": None,
    }
    assert rows[0]["topic_reclassification"]["reclassification_id"] == "topic-1"
    assert rows[0]["topic_reclassification"]["original_topic"] == event["topic"]
    assert cursor["mode"] == "feedback_projection"
    assert cursor["events"]["count"] == 1
    assert cursor["reclassifications"]["count"] == 1


def test_feedback_applies_sequential_topic_reclassifications_in_file_order(
    tmp_path: Path,
) -> None:
    events = tmp_path / "events.jsonl"
    corrections = tmp_path / "topic_reclassifications.jsonl"
    event = _feedback_event("event-1", "sha256:" + "1" * 64, "episode")
    _write_jsonl(events, [event])
    _write_jsonl(
        corrections,
        [
            _topic_correction("topic-1", "event-1", "金融与市场", "科技与产品"),
            _topic_correction("topic-2", "event-1", "科技与产品", "学习与成长"),
        ],
    )

    rows, cursor = load_feedback(events, corrections)

    assert rows[0]["topic"]["label"] == "学习与成长"
    assert rows[0]["topic_reclassification"]["reclassification_id"] == "topic-2"
    assert rows[0]["topic_reclassification"]["original_topic"] == event["topic"]
    assert cursor["reclassifications"]["count"] == 2
    assert cursor["reclassifications"]["last_event_hash"]
    assert cursor["reclassifications"]["sequence_hash"]


def test_feedback_reclassification_rejects_wrong_old_topic(tmp_path: Path) -> None:
    events = tmp_path / "events.jsonl"
    corrections = tmp_path / "topic_reclassifications.jsonl"
    _write_jsonl(events, [_feedback_event("event-1", "sha256:" + "1" * 64, "episode")])
    _write_jsonl(corrections, [_topic_correction("topic-1", "event-1", "错误主题", "科技与产品")])

    with pytest.raises(ValueError, match="old_topic does not match"):
        load_feedback(events, corrections)


def test_feedback_reclassification_rejects_unknown_event(tmp_path: Path) -> None:
    events = tmp_path / "events.jsonl"
    corrections = tmp_path / "topic_reclassifications.jsonl"
    _write_jsonl(events, [_feedback_event("event-1", "sha256:" + "1" * 64, "episode")])
    _write_jsonl(corrections, [_topic_correction("topic-1", "missing", "金融与市场", "科技与产品")])

    with pytest.raises(ValueError, match="unknown event"):
        load_feedback(events, corrections)


def test_feedback_reclassification_rejects_conflicting_duplicate_id(
    tmp_path: Path,
) -> None:
    events = tmp_path / "events.jsonl"
    corrections = tmp_path / "topic_reclassifications.jsonl"
    _write_jsonl(events, [_feedback_event("event-1", "sha256:" + "1" * 64, "episode")])
    first = _topic_correction("topic-1", "event-1", "金融与市场", "科技与产品")
    conflicting = dict(first, reason="different")
    _write_jsonl(corrections, [first, conflicting])

    with pytest.raises(ValueError, match="authority conflict"):
        load_feedback(events, corrections)


def test_feedback_reclassification_rejects_wrong_schema(tmp_path: Path) -> None:
    events = tmp_path / "events.jsonl"
    corrections = tmp_path / "topic_reclassifications.jsonl"
    _write_jsonl(events, [_feedback_event("event-1", "sha256:" + "1" * 64, "episode")])
    invalid = _topic_correction("topic-1", "event-1", "金融与市场", "科技与产品")
    invalid["schema"] = "intent-feedback.v1"
    _write_jsonl(corrections, [invalid])

    with pytest.raises(ValueError, match="unexpected ledger schema"):
        load_feedback(events, corrections)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda row: row.pop("reclassification_id"), "requires reclassification_id"),
        (lambda row: row.pop("new_topic"), "invalid new_topic"),
        (lambda row: row["old_topic"].pop("slug"), "invalid old_topic"),
    ],
)
def test_feedback_reclassification_rejects_missing_identity_or_topic(
    tmp_path: Path,
    mutation,
    message: str,
) -> None:
    events = tmp_path / "events.jsonl"
    corrections = tmp_path / "topic_reclassifications.jsonl"
    _write_jsonl(events, [_feedback_event("event-1", "sha256:" + "1" * 64, "episode")])
    invalid = _topic_correction("topic-1", "event-1", "金融与市场", "科技与产品")
    mutation(invalid)
    _write_jsonl(corrections, [invalid])

    with pytest.raises(ValueError, match=message):
        load_feedback(events, corrections)
