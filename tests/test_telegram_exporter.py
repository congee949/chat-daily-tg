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
        (3707563960, "CuiMao爱学习", 1, "Alice", "有效信息 https://x.com/a", "2026-04-28T02:00:00+00:00", None),
        (3707563960, "CuiMao爱学习", 2, "Bob", "😂", "2026-04-28T03:00:00+00:00", None),
        (3707563960, "CuiMao爱学习", 3, "Carol", "", "2026-04-28T04:00:00+00:00", None),
        (3707563960, "CuiMao爱学习", 4, "Dan", "前一天消息", "2026-04-27T02:00:00+00:00", None),
        (1162433032, "Other", 5, "Eve", "其他群", "2026-04-28T02:00:00+00:00", None),
    ]
    conn.executemany("INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
    conn.commit()
    conn.close()


def test_canonical_chat_ids_accepts_negative_supergroup_id():
    ids = canonical_chat_ids("-1003707563960")
    assert -1003707563960 in ids
    assert 3707563960 in ids
    assert 1003707563960 in ids


def test_should_skip_empty_and_low_signal_content():
    assert should_skip_content("")
    assert should_skip_content("😂")
    assert should_skip_content("+1")
    assert not should_skip_content("有效信息 https://example.com")


def test_export_chat_reads_sqlite_window_and_renders_source_tags(tmp_path: Path):
    db_path = tmp_path / "messages.db"
    out_path = tmp_path / "telegram-CuiMao.md"
    _make_db(db_path)

    result = export_chat(
        chat_id="-1003707563960",
        chat_name="CuiMao爱学习",
        since="2026-04-28",
        until="2026-04-29",
        out_path=out_path,
        db_path=db_path,
        limit=50,
        sync_before_export=False,
    )

    assert result.message_count == 1
    assert result.skipped_count == 2
    assert "[Telegram / CuiMao爱学习 / 10:00 / Alice] 有效信息 https://x.com/a" in result.content
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
            chat_id="-1003707563960",
            chat_name="CuiMao爱学习",
            since="2026-04-28",
            until="2026-04-29",
            out_path=out_path,
            db_path=db_path,
            limit=50,
            sync_before_export=True,
        )

    assert run.call_args[0][0] == ["tg", "sync", "-n", "50", "--", "-1003707563960"]
