import logging

from chat_daily_tg.logging_setup import configure_logging


def test_configure_logging_suppresses_http_client_info_logs(tmp_path):
    configure_logging(tmp_path / "run.log")

    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING


def test_configure_logging_redacts_bot_token(tmp_path):
    log_file = tmp_path / "run.log"
    configure_logging(log_file)
    token = "1234567890:AAFAKEfake_TOKEN_for_test_only_000000000"
    try:
        raise RuntimeError(
            f"Client error for url 'https://api.telegram.org/bot{token}/sendMessage'"
        )
    except Exception as e:
        logging.getLogger("redact-test").exception("pipeline failed: %s", e)
    logging.shutdown()
    content = log_file.read_text(encoding="utf-8")
    assert token not in content
    assert "8307375018:AA" not in content   # not even a fragment, incl. traceback
    assert "<REDACTED_TG_TOKEN>" in content
