"""Hermetic CLI-to-Telegram-HTTP tests for the channel forwarder.

These tests use the real CLI, YAML parsing, SQLite exporter, archive writer,
SeenStore, card builder and TelegramSender. Only the external Telegram HTTP
socket is replaced with httpx.MockTransport; no real credentials or network are
used.
"""
from __future__ import annotations

from datetime import date, datetime, time, timezone
import json
import os
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo

import httpx
import pytest


CHAT_ID = "-100123"
MESSAGE_IDS = (101, 102)
SEEN_KEYS = [f"{CHAT_ID}:{message_id}" for message_id in MESSAGE_IDS]


def _write_messages_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            """
            CREATE TABLE messages (
                chat_id INTEGER,
                chat_name TEXT,
                msg_id INTEGER,
                sender_name TEXT,
                content TEXT,
                timestamp TEXT,
                raw_json TEXT
            )
            """
        )
        timestamp = datetime.combine(
            date.today(), time(hour=12), tzinfo=ZoneInfo("Asia/Shanghai")
        ).astimezone(timezone.utc).isoformat()
        conn.executemany(
            """
            INSERT INTO messages
                (chat_id, chat_name, msg_id, sender_name, content, timestamp, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    int(CHAT_ID),
                    "Demo channel",
                    MESSAGE_IDS[0],
                    "Demo sender",
                    "Hello <world> from the hermetic E2E",
                    timestamp,
                    "{}",
                ),
                (
                    int(CHAT_ID),
                    "Demo channel",
                    MESSAGE_IDS[1],
                    "Demo sender",
                    "",
                    timestamp,
                    "{}",
                ),
            ],
        )
        conn.commit()
    finally:
        conn.close()


def _write_config(path: Path, db_path: Path) -> None:
    path.write_text(
        f"""
models:
  summary:
    endpoint: "https://llm.invalid/v1"
    model: "unused-e2e-model"
    api_key_env: "E2E_LLM_KEY"
telegram:
  bot_token_env: "E2E_TG_BOT_TOKEN"
  chat_id_env: "E2E_TG_CHAT_ID"
retry:
  max_attempts: 1
  backoff_seconds: [0]
sources:
  telegram:
    enabled: true
    db_path: "{db_path}"
    sync_before_export: false
    raw_card_delay_seconds: 0
    dedup:
      content:
        enabled: false
      topic:
        enabled: false
    raw_channels:
      - id: "{CHAT_ID}"
        name: "Demo channel"
        username: "demo_channel"
        dedup: false
        topic: "channels_news"
""".lstrip(),
        encoding="utf-8",
    )


def _run_cli_with_transport(monkeypatch, tmp_path: Path, handler) -> tuple[int, Path, Path]:
    from chat_daily_tg import application, paths, tg_sender
    from chat_daily_tg.cli import main

    data_dir = tmp_path / "chat-daily"
    archive_dir = data_dir / "archive"
    log_dir = data_dir / "logs"
    config_path = data_dir / "config.yaml"
    db_path = tmp_path / "messages.db"
    route_path = tmp_path / "tg-targets.json"
    data_dir.mkdir(parents=True)

    _write_messages_db(db_path)
    _write_config(config_path, db_path)
    route_path.write_text(
        json.dumps({"chat_id": "-100999", "topics": {"channels_news": 321}}),
        encoding="utf-8",
    )

    monkeypatch.setattr(paths, "DATA_DIR", data_dir)
    monkeypatch.setattr(paths, "ARCHIVE_DIR", archive_dir)
    monkeypatch.setattr(paths, "LOG_DIR", log_dir)
    monkeypatch.setattr(application, "DATA_DIR", data_dir)
    monkeypatch.setattr(application, "CONFIG_PATH", config_path)
    monkeypatch.setattr(application, "TG_TARGETS", str(route_path))

    monkeypatch.setenv("E2E_LLM_KEY", "unused-placeholder")
    monkeypatch.setenv("E2E_TG_BOT_TOKEN", "TEST_TOKEN")
    monkeypatch.setenv("E2E_TG_CHAT_ID", "999")
    monkeypatch.setenv("ALL_PROXY", "socks5://127.0.0.1:9")
    monkeypatch.setenv("all_proxy", "socks5://127.0.0.1:9")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    real_sender = tg_sender.TelegramSender

    def sender_factory(**kwargs):
        return real_sender(**kwargs, client=client)

    monkeypatch.setattr(tg_sender, "TelegramSender", sender_factory)
    try:
        exit_code = main(["channels", "run"])
    finally:
        client.close()

    assert "ALL_PROXY" not in os.environ
    assert "all_proxy" not in os.environ
    return exit_code, data_dir / "raw_channel_seen.txt", archive_dir


@pytest.mark.e2e
def test_channels_cli_success_is_write_after_send(monkeypatch, tmp_path):
    requests: list[dict] = []
    seen_path = tmp_path / "chat-daily" / "raw_channel_seen.txt"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/sendMessage")
        assert not seen_path.exists() or not any(
            key in seen_path.read_text(encoding="utf-8") for key in SEEN_KEYS
        )
        payload = json.loads(request.content)
        requests.append(payload)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 7001}})

    exit_code, actual_seen_path, archive_root = _run_cli_with_transport(
        monkeypatch, tmp_path, handler
    )

    assert exit_code == 0
    assert actual_seen_path.read_text(encoding="utf-8").splitlines() == SEEN_KEYS
    assert len(requests) == 1
    assert requests[0]["chat_id"] == "-100999"
    assert requests[0]["message_thread_id"] == 321
    assert requests[0]["parse_mode"] == "HTML"
    assert "&lt;world&gt;" in requests[0]["text"]
    archives = list(archive_root.glob("*/*/*/rawcard-Demo_channel.md"))
    assert len(archives) == 1
    assert "Hello &lt;world&gt;" in archives[0].read_text(encoding="utf-8")


@pytest.mark.e2e
def test_channels_cli_html_400_degrades_before_seen_write(monkeypatch, tmp_path):
    requests: list[dict] = []
    seen_path = tmp_path / "chat-daily" / "raw_channel_seen.txt"

    def handler(request: httpx.Request) -> httpx.Response:
        assert not seen_path.exists() or not any(
            key in seen_path.read_text(encoding="utf-8") for key in SEEN_KEYS
        )
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) == 1:
            return httpx.Response(
                400,
                json={"ok": False, "description": "Bad Request: can't parse entities"},
            )
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 7002}})

    exit_code, actual_seen_path, _archive_root = _run_cli_with_transport(
        monkeypatch, tmp_path, handler
    )

    assert exit_code == 0
    assert len(requests) == 2
    assert requests[0]["parse_mode"] == "HTML"
    assert "parse_mode" not in requests[1]
    assert "<world>" in requests[1]["text"]
    assert actual_seen_path.read_text(encoding="utf-8").splitlines() == SEEN_KEYS
