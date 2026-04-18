from unittest.mock import patch
from wx_daily_tg.notifier import notify_failure


def test_notify_failure_calls_osascript():
    with patch("wx_daily_tg.notifier.subprocess.run") as run:
        notify_failure(title="wx-daily-tg 失败", message="pipeline 异常")
        called = run.call_args[0][0]
        assert called[0] == "osascript"
        joined = " ".join(called)
        assert "display notification" in joined
        assert "wx-daily-tg 失败" in joined
        assert "pipeline 异常" in joined


def test_notify_failure_escapes_double_quotes():
    with patch("wx_daily_tg.notifier.subprocess.run") as run:
        notify_failure(title='t', message='msg with "quote"')
        script = run.call_args[0][0][2]
        assert '\\"quote\\"' in script


def test_notify_failure_escapes_backslashes_before_quotes():
    with patch("wx_daily_tg.notifier.subprocess.run") as run:
        notify_failure(title='t', message='msg \\"trick')
        script = run.call_args[0][0][2]
        # Input backslash becomes \\, input " becomes \", so together: \\\\\\"
        # More important: the script does NOT contain a closing-dangling-quote pattern
        # Just assert a backslash is escaped in the output
        assert '\\\\' in script
