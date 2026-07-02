from pytest_httpx import HTTPXMock

from chat_daily_tg.config import ImgRelay
from chat_daily_tg.img_relay import delete_image, upload_image


def _cfg() -> ImgRelay:
    return ImgRelay(
        enabled=True, account_id="acc1", namespace_id="ns1",
        worker_base="https://relay.example.workers.dev",
        api_token_env="CF_TEST_TOKEN", ttl_seconds=300,
    )


def test_upload_image_puts_to_kv_and_returns_worker_url(tmp_path, monkeypatch, httpx_mock: HTTPXMock):
    monkeypatch.setenv("CF_TEST_TOKEN", "tok")
    img = tmp_path / "a.jpg"
    img.write_bytes(b"jpegbytes")
    httpx_mock.add_response(method="PUT", json={"success": True})

    url = upload_image(_cfg(), str(img))

    assert url.startswith("https://relay.example.workers.dev/")
    key = url.rsplit("/", 1)[-1]
    assert key.endswith(".jpg") and len(key) == 48 + 4  # 24-byte hex + .jpg
    req = httpx_mock.get_request()
    assert f"/accounts/acc1/storage/kv/namespaces/ns1/values/{key}" in str(req.url)
    assert "expiration_ttl=300" in str(req.url)
    assert req.headers["Authorization"] == "Bearer tok"
    assert req.read() == b"jpegbytes"


def test_upload_image_raises_on_kv_error(tmp_path, monkeypatch, httpx_mock: HTTPXMock):
    monkeypatch.setenv("CF_TEST_TOKEN", "tok")
    img = tmp_path / "a.jpg"
    img.write_bytes(b"x")
    httpx_mock.add_response(method="PUT", json={"success": False, "errors": [{"code": 1}]})

    import pytest
    with pytest.raises(RuntimeError, match="KV put failed"):
        upload_image(_cfg(), str(img))


def test_delete_image_never_raises(monkeypatch, httpx_mock: HTTPXMock):
    monkeypatch.setenv("CF_TEST_TOKEN", "tok")
    httpx_mock.add_exception(Exception("network down"))
    delete_image(_cfg(), "https://relay.example.workers.dev/abc.jpg")  # must not raise
