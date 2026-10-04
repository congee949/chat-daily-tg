from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import importlib.util
from pathlib import Path
import sqlite3
import sys
import types

import pytest


def _load_module(monkeypatch):
    tg_cli = types.ModuleType("tg_cli")
    tg_cli.__path__ = []
    tg_client = types.ModuleType("tg_cli.client")
    tg_client._default_api_warned = False
    tg_cli.client = tg_client
    monkeypatch.setitem(sys.modules, "tg_cli", tg_cli)
    monkeypatch.setitem(sys.modules, "tg_cli.client", tg_client)

    path = Path(__file__).parents[1] / "scripts" / "tg_public_backfill.py"
    spec = importlib.util.spec_from_file_location("_tg_public_backfill_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Sender:
    first_name = "Alice"
    last_name = "Tester"
    username = "alice"


class _Message:
    def __init__(self, msg_id: int, content: str | None = None):
        self.id = msg_id
        self.date = datetime(2026, 4, 20 + msg_id - 100, tzinfo=timezone.utc)
        self.text = content if content is not None else f"message {msg_id}"
        self.message = self.text
        self.sender_id = 42
        self._sender = _Sender()
        self.sender = None
        self.grouped_id = 700 if msg_id == 101 else None
        self.fwd_from = object() if msg_id == 102 else None


class _Entity:
    id = 1234567890
    title = "Backfill channel"
    first_name = None


class _Client:
    def __init__(self, messages, *, fail=False):
        self.messages = messages
        self.fail = fail
        self.calls = []

    def iter_messages(self, entity, **kwargs):
        self.calls.append((entity, kwargs))
        selected = [m for m in self.messages if m.id > kwargs["offset_id"]]
        selected = selected[:kwargs["limit"]]

        async def _iterate():
            for message in selected:
                yield message
                if self.fail:
                    raise ConnectionError("HTTP 503")

        return _iterate()


def _max_id(db_path: Path) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute("SELECT max(msg_id) FROM messages").fetchone()[0]
    finally:
        conn.close()


def test_backlog_over_limit_pages_oldest_first_and_upserts_fields(
        tmp_path, monkeypatch):
    module = _load_module(monkeypatch)
    db_path = tmp_path / "messages.db"
    remote = [_Message(i) for i in range(101, 106)]
    client = _Client(remote)

    first = asyncio.run(module.backfill_page(
        client, _Entity(), db_path=db_path, limit=2, min_id=100))
    assert first == {
        "fetched": 2, "upserted": 2, "first_id": 101, "last_id": 102,
    }
    assert _max_id(db_path) == 102
    assert client.calls[0][1] == {
        "limit": 2, "reverse": True, "offset_id": 100,
    }

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row101 = conn.execute(
        "SELECT * FROM messages WHERE msg_id=101").fetchone()
    row102 = conn.execute(
        "SELECT * FROM messages WHERE msg_id=102").fetchone()
    conn.close()
    assert row101["platform"] == "telegram"
    assert row101["chat_id"] == _Entity.id
    assert row101["chat_name"] == _Entity.title
    assert row101["sender_id"] == 42
    assert row101["sender_name"] == "Alice Tester"
    assert row101["content"] == "message 101"
    assert row101["timestamp"].endswith("+00:00")
    assert '"grouped_id": 700' in row101["raw_json"]
    assert '"fwd_from": true' in row102["raw_json"]

    second = asyncio.run(module.backfill_page(
        client, _Entity(), db_path=db_path, limit=2, min_id=102))
    assert second["first_id"] == 103
    assert second["last_id"] == 104
    assert _max_id(db_path) == 104

    # UPSERT refreshes fields without adding a duplicate key.
    changed = module.upsert_rows(db_path, [
        module.message_row(
            _Message(104, "updated"), chat_id=_Entity.id,
            chat_name=_Entity.title,
        )
    ])
    assert changed == 1
    conn = sqlite3.connect(db_path)
    count, content = conn.execute(
        "SELECT count(*), max(content) FROM messages WHERE msg_id=104"
    ).fetchone()
    conn.close()
    assert (count, content) == (1, "updated")


def test_telethon_failure_before_complete_page_does_not_advance_db(
        tmp_path, monkeypatch):
    module = _load_module(monkeypatch)
    db_path = tmp_path / "messages.db"
    module.upsert_rows(db_path, [module.message_row(
        _Message(100), chat_id=_Entity.id, chat_name=_Entity.title)])
    client = _Client([_Message(101), _Message(102)], fail=True)

    with pytest.raises(ConnectionError, match="503"):
        asyncio.run(module.backfill_page(
            client, _Entity(), db_path=db_path, limit=2, min_id=100))

    assert _max_id(db_path) == 100
