from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from unittest.mock import patch, MagicMock

import pytest

from chat_daily_tg.telegram_exporter import (
    canonical_chat_ids,
    export_chat,
    should_skip_content,
    sync_many_chats,
)


def _make_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE messages (
            chat_id INTEGER NOT NULL,
            chat_name TEXT,
            msg_id INTEGER NOT NULL,
            sender_name TEXT,
            content TEXT,
            timestamp TEXT NOT NULL,
            raw_json TEXT
        )
        """
    )
    rows = [
        (1234567890, "示例TG群A", 1, "Alice", "有效信息 https://x.com/a", "2026-04-28T02:00:00+00:00", None),
        (1234567890, "示例TG群A", 2, "Bob", "😂", "2026-04-28T03:00:00+00:00", None),
        (1234567890, "示例TG群A", 3, "Carol", "", "2026-04-28T04:00:00+00:00", None),
        (1234567890, "示例TG群A", 4, "Dan", "前一天消息", "2026-04-27T02:00:00+00:00", None),
        (9876543210, "Other", 5, "Eve", "其他群", "2026-04-28T02:00:00+00:00", None),
    ]
    conn.executemany("INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
    conn.commit()
    conn.close()


def test_canonical_chat_ids_accepts_negative_supergroup_id():
    ids = canonical_chat_ids("-1001234567890")
    assert -1001234567890 in ids
    assert 1234567890 in ids
    assert 1001234567890 in ids


def test_read_messages_keeps_newest_when_over_limit(tmp_path: Path):
    from chat_daily_tg.telegram_exporter import read_messages
    db_path = tmp_path / "m.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE messages (chat_id INTEGER, chat_name TEXT, msg_id INTEGER, "
        "sender_name TEXT, content TEXT, timestamp TEXT, raw_json TEXT)"
    )
    # 5 in-window messages: window [2026-04-28 00:00 +08:00) == [2026-04-27T16:00Z, ...)
    # so 17:00–21:00 UTC all fall inside 2026-04-28 local; msg_id ascending with time.
    rows = [
        (1234567890, "C", i, "s", f"m{i}", f"2026-04-27T{16 + i:02d}:00:00+00:00", None)
        for i in range(1, 6)
    ]
    conn.executemany("INSERT INTO messages VALUES (?,?,?,?,?,?,?)", rows)
    conn.commit(); conn.close()
    got = read_messages(db_path=db_path, chat_id="1234567890",
                        since="2026-04-28", until="2026-04-29", limit=2)
    ids = [r["msg_id"] for r in got]
    assert ids == [4, 5]  # newest 2 kept, returned ASC for rendering
    # incremental: only msg_id > high-water-mark
    inc = read_messages(db_path=db_path, chat_id="1234567890",
                        since="2026-04-28", until="2026-04-29", limit=10, min_msg_id=3)
    assert [r["msg_id"] for r in inc] == [4, 5]
    # incremental over limit: keep the OLDEST page above the mark, not the newest —
    # otherwise the seen store's high-water mark would jump past unfetched rows
    # (3 and 4 here) and skip them forever. The remainder comes next run.
    inc_paged = read_messages(db_path=db_path, chat_id="1234567890",
                              since="2026-04-28", until="2026-04-29", limit=2, min_msg_id=2)
    assert [r["msg_id"] for r in inc_paged] == [3, 4]


def test_incremental_read_catches_backlog_older_than_rolling_window(tmp_path: Path):
    """The HWM, not yesterday's date, is the incremental lower bound."""
    from chat_daily_tg.telegram_exporter import read_messages

    db_path = tmp_path / "m.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE messages (chat_id INTEGER, chat_name TEXT, msg_id INTEGER, "
        "sender_name TEXT, content TEXT, timestamp TEXT, raw_json TEXT)"
    )
    conn.executemany(
        "INSERT INTO messages VALUES (?,?,?,?,?,?,?)",
        [
            # Both rows are >2 days before `since`, as after a long sync outage.
            (1234567890, "C", 101, "s", "old backlog 1",
             "2026-04-24T02:00:00+00:00", None),
            (1234567890, "C", 102, "s", "old backlog 2",
             "2026-04-25T02:00:00+00:00", None),
            (1234567890, "C", 103, "s", "current",
             "2026-04-28T02:00:00+00:00", None),
        ],
    )
    conn.commit()
    conn.close()

    first_page = read_messages(
        db_path=db_path, chat_id="1234567890",
        since="2026-04-28", until="2026-04-29", limit=2,
        min_msg_id=100,
    )
    assert [row["msg_id"] for row in first_page] == [101, 102]

    second_page = read_messages(
        db_path=db_path, chat_id="1234567890",
        since="2026-04-28", until="2026-04-29", limit=2,
        min_msg_id=102,
    )
    assert [row["msg_id"] for row in second_page] == [103]


def test_should_skip_empty_and_low_signal_content():
    assert should_skip_content("")
    assert should_skip_content("😂")
    assert should_skip_content("+1")
    assert not should_skip_content("有效信息 https://example.com")


def test_export_chat_reads_sqlite_window_and_renders_source_tags(tmp_path: Path):
    db_path = tmp_path / "messages.db"
    out_path = tmp_path / "telegram-example.md"
    _make_db(db_path)

    result = export_chat(
        chat_id="-1001234567890",
        chat_name="示例TG群A",
        since="2026-04-28",
        until="2026-04-29",
        out_path=out_path,
        db_path=db_path,
        limit=50,
        sync_before_export=False,
    )

    assert result.message_count == 1
    assert result.skipped_count == 2
    assert "[Telegram / 示例TG群A / 10:00 / Alice] 有效信息 https://x.com/a" in result.content
    assert "前一天消息" not in result.content
    assert "其他群" not in result.content
    assert out_path.read_text(encoding="utf-8") == result.content


def test_export_chat_filters_exact_sender_and_message_regex_before_media(tmp_path: Path):
    db_path = tmp_path / "messages.db"
    out_path = tmp_path / "telegram-filtered.md"
    _make_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.executemany(
        "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (1234567890, "示例TG群A", 6, "Group Help Bot", "系统通知有用信息",
             "2026-04-28T05:00:00+00:00", '{"photo": true}'),
            (1234567890, "示例TG群A", 7, "Alice", "请先完成入群验证",
             "2026-04-28T06:00:00+00:00", None),
            # Exact matching is intentional: a similar human sender stays.
            (1234567890, "示例TG群A", 8, "Group Help Bot 2", "正常讨论内容",
             "2026-04-28T07:00:00+00:00", None),
        ],
    )
    conn.commit()
    conn.close()

    result = export_chat(
        chat_id="-1001234567890",
        chat_name="示例TG群A",
        since="2026-04-28",
        until="2026-04-29",
        out_path=out_path,
        db_path=db_path,
        limit=50,
        sync_before_export=False,
        exclude_senders=["Group Help Bot"],
        exclude_patterns=[r"入群验证"],
    )

    assert result.message_count == 2
    assert result.skipped_count == 4
    assert "系统通知有用信息" not in result.content
    assert "请先完成入群验证" not in result.content
    assert "Group Help Bot 2] 正常讨论内容" in result.content
    assert result.media_candidates == []


def test_export_chat_include_patterns_suppress_unmatched_media_and_text(tmp_path: Path):
    db_path = tmp_path / "messages.db"
    out_path = tmp_path / "telegram-allow-list.md"
    _make_db(db_path)

    result = export_chat(
        chat_id="-1001234567890",
        chat_name="示例TG群A",
        since="2026-04-28",
        until="2026-04-29",
        out_path=out_path,
        db_path=db_path,
        limit=50,
        sync_before_export=False,
        include_patterns=[r"有效信息"],
    )

    assert result.message_count == 1
    assert result.skipped_count == 2
    assert "有效信息" in result.content
    assert "低信息 https://" not in result.content


def test_export_chat_syncs_before_export_when_enabled(tmp_path: Path):
    db_path = tmp_path / "messages.db"
    out_path = tmp_path / "out.md"
    _make_db(db_path)

    with patch("chat_daily_tg.telegram_exporter.subprocess.run") as run:
        run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        export_chat(
            chat_id="-1001234567890",
            chat_name="示例TG群A",
            since="2026-04-28",
            until="2026-04-29",
            out_path=out_path,
            db_path=db_path,
            limit=50,
            sync_before_export=True,
        )

    assert run.call_args[0][0] == ["tg", "sync", "-n", "50", "--", "-1001234567890"]


def test_export_chat_falls_back_to_local_when_sync_fails(tmp_path: Path):
    db_path = tmp_path / "messages.db"
    out_path = tmp_path / "out.md"
    _make_db(db_path)

    with patch("chat_daily_tg.telegram_exporter.subprocess.run") as run:
        run.side_effect = RuntimeError("tg sync failed: timed out after 120 seconds")
        result = export_chat(
            chat_id="-1001234567890",
            chat_name="示例TG群A",
            since="2026-04-28",
            until="2026-04-29",
            out_path=out_path,
            db_path=db_path,
            limit=50,
            sync_before_export=True,
        )

    assert result.message_count == 1
    assert "[Telegram / 示例TG群A / 10:00 / Alice] 有效信息 https://x.com/a" in result.content
    assert out_path.is_file()


def test_incremental_sync_uses_oldest_first_backfill_script(tmp_path: Path, monkeypatch):
    from chat_daily_tg import telegram_exporter

    python = tmp_path / "kabi-python"
    python.write_text("", encoding="utf-8")
    python.chmod(0o700)
    monkeypatch.setattr(telegram_exporter, "TG_CLI_PYTHON", str(python))

    with patch("chat_daily_tg.telegram_exporter.subprocess.run") as run:
        run.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
        telegram_exporter.sync_chat(
            "-1001234567890", limit=500, db_path=tmp_path / "messages.db",
            min_msg_id=100,
        )

    assert run.call_args[0][0] == [
        str(python),
        str(telegram_exporter._PUBLIC_BACKFILL_SCRIPT),
        "-1001234567890",
        str(tmp_path / "messages.db"),
        "500",
        "100",
    ]


def test_sync_many_chats_parses_structured_results_in_request_order():
    payload = {
        "ok": True,
        "schema_version": "1",
        "data": {
            "new_messages": 3,
            "requests": 2,
            "results": [
                {
                    "chat": "-1003707563960",
                    "requested_limit": 500,
                    "resolved_chat_id": 3707563960,
                    "last_msg_id": 42,
                    "synced": 3,
                    "status": "ok",
                    "error": None,
                },
                {
                    "chat": "room",
                    "requested_limit": 50,
                    "resolved_chat_id": None,
                    "last_msg_id": None,
                    "synced": 0,
                    "status": "failed",
                    "error": "missing chat",
                },
            ],
        },
    }

    with patch("chat_daily_tg.telegram_exporter.subprocess.run") as run:
        run.return_value = MagicMock(
            returncode=0, stdout=json.dumps(payload), stderr=""
        )
        results = sync_many_chats(
            [("-1003707563960", 500), ("room", 50)]
        )

    assert [item["chat"] for item in results] == [
        "-1003707563960",
        "room",
    ]
    assert [item["status"] for item in results] == ["ok", "failed"]
    assert run.call_args[0][0] == [
        "tg",
        "sync-many",
        "--request=-1003707563960=500",
        "--request=room=50",
        "--delay",
        "0",
        "--json",
    ]


def test_sync_many_chats_rejects_invalid_structured_output():
    with patch("chat_daily_tg.telegram_exporter.subprocess.run") as run:
        run.return_value = MagicMock(
            returncode=0, stdout="{not-json", stderr=""
        )
        with pytest.raises(
            RuntimeError, match="invalid structured output"
        ):
            sync_many_chats([("room", 5)])
