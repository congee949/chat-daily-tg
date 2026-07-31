import pytest


@pytest.fixture(autouse=True)
def _no_proxy_env(monkeypatch):
    # httpx.Client(trust_env=True) builds proxy transports at construction time,
    # so shell proxy vars (e.g. ALL_PROXY=socks5://...) break httpx-based tests
    # before pytest_httpx can intercept anything. Clear them for deterministic runs.
    for var in ("ALL_PROXY", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"):
        monkeypatch.delenv(var, raising=False)
        monkeypatch.delenv(var.lower(), raising=False)
    # Never let a real Telegram alert fire from notify_failure during tests.
    monkeypatch.delenv("CHAT_DAILY_TG_ALERTS", raising=False)


@pytest.fixture(autouse=True)
def _isolate_sent_ledger(monkeypatch, tmp_path):
    """Keep fake Telegram message IDs out of the live Podcast ledger."""
    from functools import partial

    from chat_daily_tg import bilibili_digest, sent_ledger, youtube_digest

    sent_ledger.clear_cache()
    ledger = tmp_path / "media_sent_ledger.jsonl"
    monkeypatch.setattr(
        sent_ledger, "MEDIA_SENT_LEDGER", ledger
    )
    # Both digest modules import append_message_ids directly, so pin their
    # call sites too instead of relying only on the module-level default.
    isolated_append = partial(sent_ledger.append_message_ids, path=ledger)
    monkeypatch.setattr(
        bilibili_digest, "append_message_ids", isolated_append
    )
    monkeypatch.setattr(
        youtube_digest, "append_message_ids", isolated_append
    )
    yield
    sent_ledger.clear_cache()
