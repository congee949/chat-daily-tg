import os

from chat_daily_tg.env import scrub_socks_proxy_env


def test_scrub_removes_socks_vars_keeps_http_proxy(monkeypatch):
    monkeypatch.setenv("ALL_PROXY", "socks5://127.0.0.1:1082")
    monkeypatch.setenv("all_proxy", "socks5://127.0.0.1:1082")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1082")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1082")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost,::1")

    scrub_socks_proxy_env()

    assert "ALL_PROXY" not in os.environ
    assert "all_proxy" not in os.environ
    # the guard scripts' working http proxy config must survive the scrub
    assert os.environ["HTTPS_PROXY"] == "http://127.0.0.1:1082"
    assert os.environ["HTTP_PROXY"] == "http://127.0.0.1:1082"
    assert os.environ["NO_PROXY"] == "127.0.0.1,localhost,::1"


def test_scrub_is_noop_when_vars_absent(monkeypatch):
    monkeypatch.delenv("ALL_PROXY", raising=False)
    monkeypatch.delenv("all_proxy", raising=False)

    scrub_socks_proxy_env()  # must not raise

    assert "ALL_PROXY" not in os.environ
    assert "all_proxy" not in os.environ


def test_httpx_client_constructs_after_scrub(monkeypatch):
    """Regression for the 2026-07-03 outage: with a socks ALL_PROXY and no
    socksio installed, httpx.Client() raises ImportError at construction.
    After the scrub it must construct cleanly."""
    import httpx

    monkeypatch.setenv("ALL_PROXY", "socks5://127.0.0.1:1082")
    scrub_socks_proxy_env()
    with httpx.Client() as client:  # would raise ImportError without the scrub
        assert client is not None
