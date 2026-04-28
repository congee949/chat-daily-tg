import logging

from chat_daily_tg.logging_setup import configure_logging


def test_configure_logging_suppresses_http_client_info_logs(tmp_path):
    configure_logging(tmp_path / "run.log")

    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING
