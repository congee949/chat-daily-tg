"""Tests for the general sent-content ledger (separate from media ledger)."""
from __future__ import annotations

import json

from chat_daily_tg.sent_content_ledger import append_message_ids, content_sha256


def _rows(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_append_writes_one_complete_row_per_target_message_id(tmp_path):
    path = tmp_path / "sent_content_ledger.jsonl"
    content = "一段需要交给 Hermes 理解的频道原文。"

    written = append_message_ids(
        [901, 902],
        chat_id="-100424841223",
        thread_id="41",
        producer="chatdaily_raw",
        source_kind="telegram_channel",
        source_ref="https://t.me/examplechan/13545",
        source_message_ids=[13545, 13546, 13547],
        url="https://example.com/original",
        content=content,
        content_id="telegram-channel:-100123:13545",
        path=path,
        sent_at="2026-08-23T09:30:00+08:00",
    )

    assert written == 2
    rows = _rows(path)
    assert [row["message_id"] for row in rows] == [901, 902]
    for row in rows:
        assert row == {
            "schema": "sent-content.v1",
            "chat_id": -100424841223,
            "thread_id": 41,
            "message_id": row["message_id"],
            "producer": "chatdaily_raw",
            "source_kind": "telegram_channel",
            "source_ref": "https://t.me/examplechan/13545",
            "source_message_ids": [13545, 13546, 13547],
            "url": "https://example.com/original",
            "content": content,
            "content_hash": content_sha256(content),
            "sent_at": "2026-08-23T09:30:00+08:00",
            "delivery_state": "confirmed",
            "content_id": "telegram-channel:-100123:13545",
        }


def test_append_preserves_null_thread_id_and_uses_only_requested_path(tmp_path):
    path = tmp_path / "nested" / "ledger.jsonl"
    written = append_message_ids(
        7,
        chat_id=123,
        thread_id=None,
        producer="test",
        source_kind="telegram_channel",
        source_ref="https://t.me/example/42",
        source_message_ids=[42],
        url="https://t.me/example/42",
        content="hello",
        path=path,
    )
    assert written == 1
    assert _rows(path)[0]["thread_id"] is None


def test_append_invalid_or_empty_delivery_writes_nothing(tmp_path):
    path = tmp_path / "ledger.jsonl"
    assert append_message_ids(
        [], chat_id=-1001, thread_id=None, producer="p",
        source_kind="telegram_channel", source_ref="https://t.me/x/1",
        source_message_ids=[1], url="https://t.me/x/1", content="x", path=path,
    ) == 0
    assert not path.exists()
