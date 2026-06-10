import pytest


@pytest.fixture(autouse=True)
def _no_proxy_env(monkeypatch):
    # httpx.Client(trust_env=True) builds proxy transports at construction time,
    # so shell proxy vars (e.g. ALL_PROXY=socks5://...) break httpx-based tests
    # before pytest_httpx can intercept anything. Clear them for deterministic runs.
    for var in ("ALL_PROXY", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"):
        monkeypatch.delenv(var, raising=False)
        monkeypatch.delenv(var.lower(), raising=False)
