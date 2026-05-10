from __future__ import annotations

from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import run_channel_daily
from chat_daily_tg.channel_daily import ChannelConfig, ChannelExport, SourceItem


class FakeConfig:
    class Sources:
        class Telegram:
            db_path = ":memory:"
            sync_before_export = False
        telegram = Telegram()
    class Models:
        class Summary:
            endpoint = "https://llm.example"
            model = "test-model"
            api_key_env = "LLM_KEY"
            max_tokens = 1000
            timeout = 30.0
            extra_body = {}
        summary = Summary()
    class Telegram:
        bot_token_env = "TG_BOT_TOKEN"
        chat_id_env = "TG_CHAT_ID"
    class Retry:
        max_attempts = 1
        backoff_seconds = [0]
    sources = Sources()
    models = Models()
    telegram = Telegram()
    retry = Retry()


def test_yesterday_iso_uses_previous_local_date():
    with patch.object(run_channel_daily, "date") as fake_date:
        fake_date.today.return_value = date(2026, 5, 9)
        fake_date.side_effect = lambda *args, **kwargs: date(*args, **kwargs)
        assert run_channel_daily.yesterday_iso() == "2026-05-08"


def test_run_dry_run_archives_but_does_not_send(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("LLM_KEY", "llm-key")
    monkeypatch.setenv("TG_BOT_TOKEN", "bot-token")
    monkeypatch.setenv("TG_CHAT_ID", "123")
    export = ChannelExport(
        config=ChannelConfig(id="1", name="投机之路", section="市场 / 投机"),
        content="[Telegram / 投机之路 / 09:30 / unknown] BTC 波动加大",
        message_count=1,
        skipped_count=0,
        sources=[SourceItem(ref="C01-001", channel="投机之路", msg_id=101, timestamp="2026-05-08T01:30:00+00:00", text="BTC 波动加大")],
    )

    with patch.object(run_channel_daily, "DATA_DIR", tmp_path), \
         patch.object(run_channel_daily, "load_env_file"), \
         patch.object(run_channel_daily, "load_config", return_value=FakeConfig()), \
         patch.object(run_channel_daily, "collect_channel_exports", return_value=[export]), \
         patch.object(run_channel_daily, "LLMClient") as llm_cls, \
         patch.object(run_channel_daily, "TelegramSender") as sender_cls:
        llm_cls.return_value.chat.return_value = (
            "📡 昨日频道推送｜2026-05-08\n\n## 市场 / 投机\n- BTC 波动加大，关注风险。",
            {"total_tokens": 12},
        )

        code = run_channel_daily._run("2026-05-08", dry_run=True)

    assert code == 0
    assert (tmp_path / "archive" / "2026" / "05" / "08" / "channel-daily-summary.md").exists()
    sender_cls.assert_not_called()


def test_run_normal_sends_summary(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("LLM_KEY", "llm-key")
    monkeypatch.setenv("TG_BOT_TOKEN", "bot-token")
    monkeypatch.setenv("TG_CHAT_ID", "123")
    export = ChannelExport(
        config=ChannelConfig(id="1", name="投机之路", section="市场 / 投机"),
        content="[Telegram / 投机之路 / 09:30 / unknown] BTC 波动加大",
        message_count=1,
        skipped_count=0,
        sources=[SourceItem(ref="C01-001", channel="投机之路", msg_id=101, timestamp="2026-05-08T01:30:00+00:00", text="BTC 波动加大")],
    )

    with patch.object(run_channel_daily, "DATA_DIR", tmp_path), \
         patch.object(run_channel_daily, "load_env_file"), \
         patch.object(run_channel_daily, "load_config", return_value=FakeConfig()), \
         patch.object(run_channel_daily, "collect_channel_exports", return_value=[export]), \
         patch.object(run_channel_daily, "LLMClient") as llm_cls, \
         patch.object(run_channel_daily, "TelegramSender") as sender_cls:
        llm_cls.return_value.chat.return_value = (
            "📡 昨日频道推送｜2026-05-08\n\n## 市场 / 投机\n- BTC 波动加大，关注风险。",
            {"total_tokens": 12},
        )
        sender = MagicMock()
        sender_cls.return_value = sender

        code = run_channel_daily._run("2026-05-08", dry_run=False)

    assert code == 0
    sender.send.assert_called_once()
    assert sender.send.call_args.kwargs["parse_mode"] == "HTML"


def test_channel_launchd_template_runs_channel_daily_script():
    path = Path("launchd/com.chat-daily-tg-channel.agent.plist")
    text = path.read_text(encoding="utf-8")

    assert "com.chat-daily-tg-channel.agent" in text
    assert "run_channel_daily.py" in text
    assert "<integer>8</integer>" in text
    assert "<integer>10</integer>" in text
