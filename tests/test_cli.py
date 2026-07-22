from __future__ import annotations

import pytest

from chat_daily_tg import application as legacy
from chat_daily_tg import cli


def test_daily_command_dispatches_explicit_arguments(monkeypatch):
    captured = {}
    monkeypatch.setattr(cli.runtime, "prepare_process", lambda: None)
    monkeypatch.setattr(cli.daily, "run", lambda **kwargs: captured.update(kwargs) or 17)

    rc = cli.main([
        "daily", "run", "--date", "2026-07-21", "--model", "gemini",
        "--no-push", "--skip-if-done", "--wait-for-wake", "--wake-deadline", "12:30",
    ])

    assert rc == 17
    assert captured == {
        "date": "2026-07-21", "model": "gemini", "no_push": True,
        "skip_if_done": True, "wait_for_wake": True, "wake_deadline": "12:30",
    }


def test_channels_resend_and_growth_mine_are_separate_commands(monkeypatch):
    calls = []
    monkeypatch.setattr(cli.runtime, "prepare_process", lambda: None)
    monkeypatch.setattr(cli.channels, "resend", lambda spec: calls.append(("resend", spec)) or 0)
    monkeypatch.setattr(cli.growth, "mine", lambda **kwargs: calls.append(("mine", kwargs)) or 0)

    assert cli.main(["channels", "resend", "-1001:88"]) == 0
    assert cli.main(["growth", "mine", "--date", "2026-07-20", "--model", "summary-fast"]) == 0
    assert calls == [
        ("resend", "-1001:88"),
        ("mine", {"date": "2026-07-20", "model": "summary-fast"}),
    ]


def test_channels_resend_accepts_negative_chat_id_from_real_process_argv(monkeypatch):
    calls = []
    monkeypatch.setattr(cli.runtime, "prepare_process", lambda: None)
    monkeypatch.setattr(cli.channels, "resend", lambda spec: calls.append(spec) or 0)
    monkeypatch.setattr(cli.sys, "argv", ["chat-daily", "channels", "resend", "-1001:88"])

    assert cli.main() == 0
    assert calls == ["-1001:88"]


def test_legacy_flags_reject_ambiguous_pipeline_selection(monkeypatch):
    monkeypatch.setattr(legacy, "prepare_process", lambda: None)
    with pytest.raises(SystemExit) as exc:
        legacy.legacy_main(["--channels-only", "--youtube-only"])
    assert exc.value.code == 2
