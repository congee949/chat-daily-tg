from pytest_httpx import HTTPXMock
import pytest
import httpx
from wx_daily_tg.tg_sender import TelegramSender, split_message


def test_split_message_short_returns_single_chunk():
    out = split_message("short", limit=4096)
    assert out == ["short"]


def test_split_message_long_splits_on_newline_boundary():
    para = "\n".join(["A" * 100] * 50)   # 50 lines of 100 chars + newlines
    chunks = split_message(para, limit=500)
    assert len(chunks) > 1
    assert all(len(c) <= 500 for c in chunks)
    assert "\n".join(chunks).replace("\n\n", "\n").startswith("A" * 100)


def test_send_message_calls_telegram_api(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        url="https://api.telegram.org/bot-TOKEN-/sendMessage",
        method="POST",
        json={"ok": True, "result": {"message_id": 1}},
    )
    s = TelegramSender(bot_token="-TOKEN-", chat_id="12345")
    s.send("hello")
    req = httpx_mock.get_request()
    body = req.read().decode()
    assert "chat_id=12345" in body
    assert "text=hello" in body


@pytest.mark.httpx_mock(can_send_already_matched_responses=True)
def test_send_long_message_splits_into_multiple_calls(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        url="https://api.telegram.org/bot-TOKEN-/sendMessage",
        method="POST",
        json={"ok": True, "result": {"message_id": 1}},
    )
    s = TelegramSender(bot_token="-TOKEN-", chat_id="12345")
    text = ("X" * 4000 + "\n") * 3   # ~12000 chars, needs >=3 chunks
    s.send(text)
    reqs = httpx_mock.get_requests()
    assert len(reqs) >= 3


@pytest.mark.httpx_mock(can_send_already_matched_responses=True)
def test_send_raises_on_http_error(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        url="https://api.telegram.org/bot-TOKEN-/sendMessage",
        method="POST",
        status_code=429,
    )
    s = TelegramSender(bot_token="-TOKEN-", chat_id="12345", retry_backoff_seconds=[0, 0, 0])
    with pytest.raises(httpx.HTTPStatusError):
        s.send("hello")


@pytest.mark.httpx_mock(can_send_already_matched_responses=True)
def test_send_raises_on_ok_false(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        url="https://api.telegram.org/bot-TOKEN-/sendMessage",
        method="POST",
        json={"ok": False, "description": "Bad Request: chat not found"},
    )
    s = TelegramSender(bot_token="-TOKEN-", chat_id="12345", retry_backoff_seconds=[0, 0, 0])
    with pytest.raises(RuntimeError, match="Telegram API error"):
        s.send("hello")
