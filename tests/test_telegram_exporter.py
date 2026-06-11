from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from unittest.mock import patch, MagicMock

from chat_daily_tg.telegram_exporter import (
    canonical_chat_ids,
    export_chat,
    should_skip_content,
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
