"""Tests for media sent-ledger (message_id → URL)."""
from pathlib import Path

from chat_daily_tg.sent_ledger import (
    append_message_ids,
    append_sent,
    clear_cache,
    content_ids,
    lookup,
)


def test_append_and_lookup(tmp_path):
    clear_cache()
    path = tmp_path / "ledger.jsonl"
    row = append_sent(
        chat_id=-1004424841223,
        message_id=99,
        thread_id=486,
        url="https://www.bilibili.com/video/BV1test",
        producer="bilibili",
        content_id="bilibili:BV1test",
        path=path,
    )
    assert row is not None
    assert row["message_id"] == 99
    hit = lookup(-1004424841223, 99, path=path)
    assert hit is not None
    assert hit["url"].endswith("BV1test")
    assert hit["producer"] == "bilibili"
    assert hit["thread_id"] == 486
    assert lookup(-1004424841223, 100, path=path) is None


def test_append_message_ids_multi(tmp_path):
    clear_cache()
    path = tmp_path / "ledger.jsonl"
    n = append_message_ids(
        [10, 11],
        chat_id=-1001,
        url="https://www.youtube.com/watch?v=abcdefghijk",
        producer="youtube",
        thread_id=2009,
        content_id="youtube:abcdefghijk",
        path=path,
    )
    assert n == 2
    assert lookup(-1001, 10, path=path)["url"].startswith("https://www.youtube.com")
    assert lookup(-1001, 11, path=path)["id"] == "youtube:abcdefghijk"


def test_append_skips_bad_ids(tmp_path):
    clear_cache()
    path = tmp_path / "ledger.jsonl"
    assert append_sent(chat_id="x", message_id=1, url="http://u", producer="bilibili",
                       path=path) is None
    assert not path.exists() or path.read_text() == ""


def test_content_ids_skips_truncated_line_and_filters_producer(tmp_path):
    clear_cache()
    path = tmp_path / "ledger.jsonl"
    append_sent(
        chat_id=-1001, message_id=1, url="https://www.bilibili.com/video/BV1a",
        producer="bilibili", content_id="bilibili:BV1a", path=path,
    )
    append_sent(
        chat_id=-1001, message_id=2, url="https://www.youtube.com/watch?v=abc",
        producer="youtube", content_id="youtube:abc", path=path,
    )
    with path.open("a", encoding="utf-8") as fh:
        fh.write('{"chat_id":')
    assert content_ids(path=path) == {"bilibili:BV1a", "youtube:abc"}
    assert content_ids(producer="bilibili", path=path) == {"bilibili:BV1a"}
