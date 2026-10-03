from __future__ import annotations

from pathlib import Path
import sqlite3

from chat_daily_tg.config import RawChannel
from chat_daily_tg.raw_channels import push_raw_channel_cards
from chat_daily_tg.raw_seen import SeenStore


class _Sender:
    def __init__(self):
        self.cards: list[str] = []

    def send_card(self, text_html: str, link=None):
        self.cards.append(text_html)
        return [9001]


def _messages_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE messages (chat_id INTEGER, chat_name TEXT, msg_id INTEGER, "
        "sender_name TEXT, content TEXT, timestamp TEXT, raw_json TEXT)"
    )
    conn.execute(
        "INSERT INTO messages VALUES (?,?,?,?,?,?,?)",
        (1234567890, "Catchup", 101, "sender",
         "跨窗补抓正文，长度足够且在抓取恢复后必须正常投递。",
         "2026-04-24T02:00:00+00:00", None),
    )
    conn.commit()
    conn.close()


def test_sync_failure_does_not_advance_hwm_and_next_run_catches_old_row(
        tmp_path, monkeypatch):
    """A pre-export outage must leave the durable cursor unchanged."""
    from chat_daily_tg import raw_channels

    db_path = tmp_path / "messages.db"
    _messages_db(db_path)
    seen_path = tmp_path / "seen.txt"
    seen = SeenStore(seen_path)
    seen.add("-1001234567890:100")
    channel = RawChannel(
        id="-1001234567890", name="Catchup", username="catchup_channel")
    sender = _Sender()
    sync_calls = []

    def _failed_sync(*args, **kwargs):
        sync_calls.append((args, kwargs))
        raise RuntimeError("HTTP 503")

    monkeypatch.setattr(raw_channels, "sync_chat", _failed_sync)
    first = push_raw_channel_cards(
        channels=[channel], since="2026-04-28", until="2026-04-29",
        db_path=db_path, sender=sender, archive_dir=tmp_path / "archive",
        seen_path=seen_path, incremental=True, delay_seconds=0,
    )
    assert first == 0
    assert sync_calls == [((channel.id,), {
        "limit": channel.limit, "db_path": db_path, "min_msg_id": 100,
    })]
    assert SeenStore(seen_path).max_msg_id(channel.id) == 100
    assert sender.cards == []

    monkeypatch.setattr(raw_channels, "sync_chat", lambda *_a, **_k: None)
    second = push_raw_channel_cards(
        channels=[channel], since="2026-04-28", until="2026-04-29",
        db_path=db_path, sender=sender, archive_dir=tmp_path / "archive",
        seen_path=seen_path, incremental=True, delay_seconds=0,
    )
    assert second == 1
    assert len(sender.cards) == 1
    assert SeenStore(seen_path).max_msg_id(channel.id) == 101


def test_private_dump_failure_does_not_advance_hwm(tmp_path, monkeypatch):
    from chat_daily_tg import private_media

    seen_path = tmp_path / "seen.txt"
    SeenStore(seen_path).add("-1001234567890:100")
    channel = RawChannel(id="-1001234567890", name="Private")

    monkeypatch.setattr(
        private_media, "push_private_channel",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("HTTP 503")),
    )
    pushed = push_raw_channel_cards(
        channels=[channel], since="2026-04-28", until="2026-04-29",
        db_path=tmp_path / "unused.db", sender=_Sender(),
        archive_dir=tmp_path / "archive", seen_path=seen_path,
        incremental=True, delay_seconds=0,
    )
    assert pushed == 0
    assert SeenStore(seen_path).max_msg_id(channel.id) == 100
