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
    assert "1234567890:AA" not in content   # not even a fragment, incl. traceback
    assert "<REDACTED_TG_TOKEN>" in content


def test_configure_logging_survives_unwritable_log_path(tmp_path):
    blocked = tmp_path / "not-a-dir"
    blocked.write_text("x", encoding="utf-8")
    configure_logging(blocked / "nested.log")
    logging.getLogger("enospc-log").info("must not raise")


def test_configure_logging_redacts_bearer_and_api_keys(tmp_path):
    from chat_daily_tg.logging_setup import redact
    sample = (
        "Authorization: Bearer sk-abcDEF1234567890xxxx "
        "AIzaSyA-test-google-key-123456789012 Cookie: session=secret"
    )
    out = redact(sample)
    assert "sk-abcDEF" not in out
    assert "AIzaSyA" not in out
    assert "session=secret" not in out
    assert "<REDACTED_SECRET>" in out
