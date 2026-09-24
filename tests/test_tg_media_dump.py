from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import io
import importlib.util
import json
from pathlib import Path
import sys
import types


def _load_dump_module(monkeypatch):
    """Load the script without requiring the production kabi-tg-cli venv."""
    tg_cli = types.ModuleType("tg_cli")
    tg_cli.__path__ = []
    tg_client = types.ModuleType("tg_cli.client")
    tg_client._default_api_warned = False
    tg_cli.client = tg_client

    telethon = types.ModuleType("telethon")
    telethon.__path__ = []
    extensions = types.ModuleType("telethon.extensions")
    extensions.__path__ = []
    html = types.ModuleType("telethon.extensions.html")
    html.unparse = lambda text, entities: text
    extensions.html = html
    telethon.extensions = extensions

    monkeypatch.setitem(sys.modules, "tg_cli", tg_cli)
    monkeypatch.setitem(sys.modules, "tg_cli.client", tg_client)
    monkeypatch.setitem(sys.modules, "telethon", telethon)
    monkeypatch.setitem(sys.modules, "telethon.extensions", extensions)
    monkeypatch.setitem(sys.modules, "telethon.extensions.html", html)

    path = Path(__file__).parents[1] / "scripts" / "tg_media_dump.py"
    spec = importlib.util.spec_from_file_location("_tg_media_dump_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Message:
    def __init__(self, msg_id: int, iso: str):
        self.id = msg_id
        self.date = datetime.fromisoformat(iso).astimezone(timezone.utc)
        self.message = f"m{msg_id}"
        self.entities = []
        self.grouped_id = None
        self.photo = None
        self.document = None


class _Client:
    def __init__(self, messages):
        self.messages = messages
        self.calls = []

    def iter_messages(self, entity, **kwargs):
        self.calls.append((entity, kwargs))

        async def _iterate():
            for message in self.messages:
                yield message

        return _iterate()


def test_incremental_private_dump_keeps_backlog_older_than_window(monkeypatch):
    module = _load_dump_module(monkeypatch)
    start = datetime(2026, 4, 28, tzinfo=timezone.utc)
    end = datetime(2026, 4, 29, tzinfo=timezone.utc)
    client = _Client([
        _Message(101, "2026-04-24T02:00:00+00:00"),
        _Message(102, "2026-04-25T02:00:00+00:00"),
    ])

    async def _collect():
        return [m async for m in module.iter_selected_messages(
            client, "channel", start=start, end=end, limit=2, min_id=100)]

    got = asyncio.run(_collect())
    assert [m.id for m in got] == [101, 102]
    assert client.calls == [("channel", {
        "limit": 2, "reverse": True, "offset_id": 100,
    })]


def test_nonincremental_private_dump_still_respects_date_window(monkeypatch):
    module = _load_dump_module(monkeypatch)
    start = datetime(2026, 4, 28, tzinfo=timezone.utc)
    end = datetime(2026, 4, 29, tzinfo=timezone.utc)
    # Non-incremental Telethon iteration is newest -> oldest.
    client = _Client([
        _Message(103, "2026-04-28T12:00:00+00:00"),
        _Message(102, "2026-04-27T23:00:00+00:00"),
    ])

    async def _collect():
        return [m async for m in module.iter_selected_messages(
            client, "channel", start=start, end=end, limit=20, min_id=0)]

    got = asyncio.run(_collect())
    assert [m.id for m in got] == [103]
    assert client.calls == [("channel", {"limit": 20, "offset_date": end})]


class _MediaMessage:
    """Fake telethon message carrying downloadable media (photo kind)."""

    def __init__(self, msg_id: int, downloader):
        self.id = msg_id
        self.date = datetime(2026, 4, 28, 2, 0, tzinfo=timezone.utc)
        self.message = f"m{msg_id}"
        self.entities = []
        self.grouped_id = None
        self.photo = object()
        self.document = None
        self._downloader = downloader

    def download_media(self, file):
        return self._downloader(self, file)


def test_download_concurrency_env_override_and_fallback(monkeypatch):
    module = _load_dump_module(monkeypatch)
    monkeypatch.delenv("CHAT_DAILY_MEDIA_CONCURRENCY", raising=False)
    assert module.download_concurrency() == 4
    monkeypatch.setenv("CHAT_DAILY_MEDIA_CONCURRENCY", "2")
    assert module.download_concurrency() == 2
    monkeypatch.setenv("CHAT_DAILY_MEDIA_CONCURRENCY", "not-a-number")
    assert module.download_concurrency() == 4
    monkeypatch.setenv("CHAT_DAILY_MEDIA_CONCURRENCY", "0")
    assert module.download_concurrency() == 4
    monkeypatch.setenv("CHAT_DAILY_MEDIA_CONCURRENCY", "-3")
    assert module.download_concurrency() == 4


def test_entries_for_downloads_concurrently_and_keeps_message_order(monkeypatch, tmp_path):
    monkeypatch.setenv("CHAT_DAILY_MEDIA_CONCURRENCY", "2")
    module = _load_dump_module(monkeypatch)
    state = {"inflight": 0, "max_inflight": 0}

    async def _scenario():
        # Two downloads must be in flight at once before ANY may finish: a
        # serial implementation deadlocks here (caught by the outer timeout)
        # instead of flaking on wall-clock timing.
        both_running = asyncio.Event()

        async def downloader(msg, file):
            state["inflight"] += 1
            state["max_inflight"] = max(state["max_inflight"], state["inflight"])
            if state["inflight"] >= 2:
                both_running.set()
            await both_running.wait()
            state["inflight"] -= 1
            return f"{file}.jpg"

        msgs = [_MediaMessage(i, downloader) for i in (1, 2, 3, 4)]
        return await module.entries_for(msgs, str(tmp_path))

    out = asyncio.run(asyncio.wait_for(_scenario(), timeout=10))
    assert [e["msg_id"] for e in out] == [1, 2, 3, 4]  # message order, not completion order
    import os
    assert [e["media"][0]["path"] for e in out] == [
        os.path.join(str(tmp_path), f"{i}.jpg") for i in (1, 2, 3, 4)
    ]
    assert state["max_inflight"] == 2  # semaphore bound respected AND concurrency real


def test_entries_for_one_failed_download_skips_only_that_file(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("CHAT_DAILY_MEDIA_CONCURRENCY", "3")
    module = _load_dump_module(monkeypatch)

    async def downloader(msg, file):
        if msg.id == 2:
            raise RuntimeError("flood wait")
        return f"{file}.jpg"

    async def _scenario():
        msgs = [_MediaMessage(i, downloader) for i in (1, 2, 3)]
        return await module.entries_for(msgs, str(tmp_path))

    out = asyncio.run(_scenario())
    assert [e["msg_id"] for e in out] == [1, 2, 3]
    assert out[0]["media"] and out[2]["media"]
    assert out[1]["media"] == []
    assert "skip media msg 2: RuntimeError: flood wait" in capsys.readouterr().err


def test_entries_for_per_file_timeout_still_applies(monkeypatch, tmp_path, capsys):
    module = _load_dump_module(monkeypatch)
    monkeypatch.setattr(module, "DOWNLOAD_TIMEOUT", 0.05)

    async def downloader(msg, file):
        if msg.id == 1:
            await asyncio.sleep(60)
        return f"{file}.jpg"

    async def _scenario():
        msgs = [_MediaMessage(i, downloader) for i in (1, 2)]
        return await module.entries_for(msgs, str(tmp_path))

    out = asyncio.run(asyncio.wait_for(_scenario(), timeout=10))
    assert out[0]["media"] == []
    assert out[1]["media"]
    assert "skip media msg 1" in capsys.readouterr().err


def test_batch_main_reuses_one_connection_and_isolates_chat_failure(
    monkeypatch, capsys, tmp_path,
):
    module = _load_dump_module(monkeypatch)

    class Client:
        def __init__(self):
            self.entity_calls = []
            self.connected = True

        async def get_entity(self, chat_id):
            self.entity_calls.append(chat_id)
            if str(chat_id) == "-1002":
                raise ValueError("unknown chat")
            return f"entity:{chat_id}"

        def iter_messages(self, entity, **kwargs):
            async def _iterate():
                # No media is needed for this protocol test; this still checks
                # the legacy date-window selection and manifest ordering.
                yield _Message(2, "2026-06-10T04:00:00+00:00")
                yield _Message(1, "2026-06-10T02:00:00+00:00")
            return _iterate()

        def is_connected(self):
            return self.connected

    client = Client()
    state = {"connects": 0}

    class Connect:
        async def __aenter__(self):
            state["connects"] += 1
            return client

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(module.tc, "connect", lambda: Connect(), raising=False)
    payload = {
        "requests": [
            {"chat_id": "-1001", "since": "2026-06-10", "until": "2026-06-11",
             "out_dir": str(tmp_path / "one"), "limit": 10},
            {"chat_id": "-1002", "since": "2026-06-10", "until": "2026-06-11",
             "out_dir": str(tmp_path / "two"), "limit": 10},
        ],
    }
    monkeypatch.setattr(module.sys, "argv", ["tg_media_dump.py", "--batch-json"])
    monkeypatch.setattr(module.sys, "stdin", io.StringIO(json.dumps(payload)))

    assert asyncio.run(module.main()) == 0
    envelope = json.loads(capsys.readouterr().out)
    results = envelope["data"]["results"]
    assert state["connects"] == 1
    assert client.entity_calls == [-1001, -1002]
    assert [result["chat_id"] for result in results] == ["-1001", "-1002"]
    assert results[0]["status"] == "ok"
    assert [entry["msg_id"] for entry in results[0]["manifest"]] == [1, 2]
    assert results[1]["status"] == "failed"
    assert results[1]["manifest"] == []


def test_batch_download_policy_keeps_manifest_order_and_downloads_newest_photos(
    monkeypatch, tmp_path,
):
    module = _load_dump_module(monkeypatch)

    class Message(_MediaMessage):
        def __init__(self, msg_id, iso, downloader):
            super().__init__(msg_id, downloader)
            self.date = datetime.fromisoformat(iso).astimezone(timezone.utc)

    downloaded = []

    async def downloader(msg, file):
        downloaded.append(msg.id)
        return f"{file}.jpg"

    # Telethon's non-incremental iterator is newest -> oldest. max_downloads=2
    # must select the newest two photos while retaining all message entries.
    msgs = [
        Message(4, "2026-06-10T04:00:00+00:00", downloader),
        Message(3, "2026-06-10T03:00:00+00:00", downloader),
        Message(2, "2026-06-10T02:00:00+00:00", downloader),
        Message(1, "2026-06-10T01:00:00+00:00", downloader),
    ]

    class Client:
        async def get_entity(self, chat_id):
            return "entity"

        def iter_messages(self, entity, **kwargs):
            async def _iterate():
                for msg in msgs:
                    yield msg
            return _iterate()

        def is_connected(self):
            return True

    out = asyncio.run(module.dump_request(Client(), {
        "chat_id": "-1001", "since": "2026-06-10", "until": "2026-06-11",
        "out_dir": str(tmp_path), "limit": 10, "min_id": 0,
        "only_ids": [], "download_kinds": {"photo"}, "max_downloads": 2,
    }))
    assert [entry["msg_id"] for entry in out] == [1, 2, 3, 4]
    assert downloaded == [4, 3]
    assert out[0]["media"] == [] and out[1]["media"] == []
    assert out[2]["media"] and out[3]["media"]


def test_incremental_download_policy_prefers_oldest_eligible_backlog(
    monkeypatch, tmp_path,
):
    module = _load_dump_module(monkeypatch)
    downloaded = []

    async def downloader(msg, file):
        downloaded.append(msg.id)
        return f"{file}.jpg"

    # Incremental iteration is oldest -> newest above the durable cursor. A
    # media cap must be spent on the head of that page, matching the ids whose
    # delivery advances the cursor, rather than skipping ahead to newer media.
    msgs = [_MediaMessage(i, downloader) for i in (101, 102, 103, 104)]

    class Client:
        async def get_entity(self, chat_id):
            return "entity"

        def iter_messages(self, entity, **kwargs):
            assert kwargs == {
                "limit": 10, "reverse": True, "offset_id": 100,
            }

            async def _iterate():
                for msg in msgs:
                    yield msg

            return _iterate()

    out = asyncio.run(module.dump_request(Client(), {
        "chat_id": "-1001", "since": "2026-04-28",
        "until": "2026-04-29", "out_dir": str(tmp_path),
        "limit": 10, "min_id": 100, "only_ids": [],
        "download_kinds": {"photo"}, "max_downloads": 2,
    }))

    assert [entry["msg_id"] for entry in out] == [101, 102, 103, 104]
    assert downloaded == [101, 102]
    assert out[0]["media"] and out[1]["media"]
    assert out[2]["media"] == [] and out[3]["media"] == []


def test_only_ids_download_policy_is_exact_stable_and_oldest_first(
    monkeypatch, tmp_path,
):
    module = _load_dump_module(monkeypatch)
    downloaded = []
    requested_ids = [104, 101, 103, 999, 102]

    async def downloader(msg, file):
        downloaded.append(msg.id)
        return f"{file}.jpg"

    # Telethon may return targeted results in request order and may use None
    # for a missing id. The manifest remains the documented ascending stable
    # order, contains no ids outside the exact request, and a future cap favors
    # the oldest eligible requested messages.
    returned = [
        _MediaMessage(104, downloader),
        _MediaMessage(101, downloader),
        _MediaMessage(103, downloader),
        None,
        _MediaMessage(102, downloader),
    ]

    class Client:
        async def get_entity(self, chat_id):
            return "entity"

        async def get_messages(self, entity, *, ids):
            assert ids == requested_ids
            return returned

    out = asyncio.run(module.dump_request(Client(), {
        "chat_id": "-1001", "since": "2000-01-01",
        "until": "2100-01-01", "out_dir": str(tmp_path),
        "limit": len(requested_ids), "min_id": 0,
        "only_ids": requested_ids, "download_kinds": {"photo"},
        "max_downloads": 2,
    }))

    assert [entry["msg_id"] for entry in out] == [101, 102, 103, 104]
    assert downloaded == [101, 102]
    assert out[0]["media"] and out[1]["media"]
    assert out[2]["media"] == [] and out[3]["media"] == []
