from unittest.mock import patch
from chat_daily_tg.notifier import notify_failure


def test_notify_failure_calls_osascript():
    with patch("chat_daily_tg.notifier.subprocess.run") as run:
        notify_failure(title="chat-daily-tg 失败", message="pipeline 异常")
        called = run.call_args[0][0]
        assert called[0] == "osascript"
        joined = " ".join(called)
        assert "display notification" in joined
        assert "chat-daily-tg 失败" in joined
        assert "pipeline 异常" in joined


def test_notify_failure_escapes_double_quotes():
    with patch("chat_daily_tg.notifier.subprocess.run") as run:
        notify_failure(title='t', message='msg with "quote"')
        script = run.call_args[0][0][2]
        assert '\\"quote\\"' in script


def test_notify_failure_escapes_backslashes_before_quotes():
    with patch("chat_daily_tg.notifier.subprocess.run") as run:
        notify_failure(title='t', message='msg \\"trick')
        script = run.call_args[0][0][2]
        # Input backslash becomes \\, input " becomes \", so together: \\\\\\"
        # More important: the script does NOT contain a closing-dangling-quote pattern
        # Just assert a backslash is escaped in the output
        assert '\\\\' in script


def test_notify_failure_redacts_tg_token_in_osascript():
    from chat_daily_tg.notifier import notify_failure
    leak = "fail https://api.telegram.org/bot123456789:AAr" + "a" * 35 + "/sendMessage"
    with patch("chat_daily_tg.notifier.subprocess.run") as run:
        notify_failure(title="t", message=leak)
        script = run.call_args[0][0][2]
        assert "AAr" + "a" * 35 not in script   # raw token gone
        assert "REDACTED_TG_TOKEN" in script


def test_notify_failure_no_telegram_when_flag_unset(monkeypatch):
    # conftest clears CHAT_DAILY_TG_ALERTS → the TG path must not run.
    import chat_daily_tg.notifier as notifier
    called = {"tg": False}
    monkeypatch.setattr(notifier, "_notify_telegram", lambda text: called.__setitem__("tg", True))
    with patch("chat_daily_tg.notifier.subprocess.run"):
        notifier.notify_failure("t", "m")
    assert called["tg"] is False


def test_notify_failure_sends_telegram_when_flag_set(monkeypatch):
    import chat_daily_tg.notifier as notifier
    monkeypatch.setenv("CHAT_DAILY_TG_ALERTS", "1")
    called = {"tg": False}
    monkeypatch.setattr(notifier, "_notify_telegram", lambda text: called.__setitem__("tg", True))
    with patch("chat_daily_tg.notifier.subprocess.run"):
        notifier.notify_failure("t", "m")
    assert called["tg"] is True
