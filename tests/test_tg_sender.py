from pytest_httpx import HTTPXMock
import pytest
import httpx
from chat_daily_tg.tg_sender import (
    TelegramSender,
    split_message,
    escape_markdown_v2,
    escape_html,
    format_markdownish_for_telegram,
    format_html_for_telegram,
)


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


def test_send_message_with_markdownv2_sets_parse_mode(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        url="https://api.telegram.org/bot-TOKEN-/sendMessage",
        method="POST",
        json={"ok": True, "result": {"message_id": 1}},
    )
    s = TelegramSender(bot_token="-TOKEN-", chat_id="12345")
    s.send("hello", parse_mode="MarkdownV2")
    req = httpx_mock.get_request()
    body = req.read().decode()
    assert "parse_mode=MarkdownV2" in body


def test_markdown_v2_escapes_special_chars():
    assert escape_markdown_v2("hello_world") == "hello\\_world"
    assert escape_markdown_v2("[]") == "\\[\\]"
    assert escape_markdown_v2("a(b)c") == "a\\(b\\)c"
    assert escape_markdown_v2("- item") == "\\- item"
    assert escape_markdown_v2("### title") == "\\#\\#\\# title"
    assert escape_markdown_v2("1. wow!") == "1\\. wow\\!"


def test_format_markdownish_for_telegram_preserves_supported_structure():
    text = "\n".join([
        "### 日期概览",
        "2026-04-17 共3个群。",
        "",
        "- **bankproduct** | 恒生开户",
        "- activity | 美团返现50元!",
    ])
    assert format_markdownish_for_telegram(text) == "\n".join([
        "*日期概览*",
        "2026\\-04\\-17 共3个群。",
        "",
        "• *bankproduct* \\| 恒生开户",
        "• activity \\| 美团返现50元\\!",
    ])


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
def test_send_markdownv2_does_not_fallback_on_parse_error(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        url="https://api.telegram.org/bot-TOKEN-/sendMessage",
        method="POST",
        json={"ok": False, "description": "Bad Request: Can't parse entities: unexpected"},
        status_code=400,
    )
    s = TelegramSender(
        bot_token="-TOKEN-",
        chat_id="12345",
        retry_max_attempts=1,
        retry_backoff_seconds=[0],
    )
    with pytest.raises(httpx.HTTPStatusError):
        s.send("hello", parse_mode="MarkdownV2")
    requests = httpx_mock.get_requests()
    assert len(requests) == 1
    first_body = requests[0].read().decode()
    assert "parse_mode=MarkdownV2" in first_body


def test_send_markdownv2_formats_message_before_send(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        url="https://api.telegram.org/bot-TOKEN-/sendMessage",
        method="POST",
        json={"ok": True, "result": {"message_id": 1}},
    )
    s = TelegramSender(bot_token="-TOKEN-", chat_id="12345")
    s.send("### 标题\n- **词**", parse_mode="MarkdownV2")
    req = httpx_mock.get_request()
    body = req.read().decode()
    assert "parse_mode=MarkdownV2" in body
    assert "text=%2A%E6%A0%87%E9%A2%98%2A%0A%E2%80%A2+%2A%E8%AF%8D%2A" in body


def test_escape_html_escapes_amp_lt_gt():
    assert escape_html("a & b < c > d") == "a &amp; b &lt; c &gt; d"
    assert escape_html("<b>x</b>") == "&lt;b&gt;x&lt;/b&gt;"


def test_format_html_for_telegram_preserves_supported_structure():
    text = "\n".join([
        "### 日期概览",
        "2026-04-17 共3个群。",
        "",
        "- **bankproduct** | 恒生开户",
        "- activity | 美团返现50元!",
    ])
    assert format_html_for_telegram(text) == "\n".join([
        "<b>日期概览</b>",
        "2026-04-17 共3个群。",
        "",
        "• <b>bankproduct</b> | 恒生开户",
        "• activity | 美团返现50元!",
    ])


def test_format_html_for_telegram_escapes_angle_brackets_in_body():
    assert format_html_for_telegram("1 < 2 && 3 > 0") == "1 &lt; 2 &amp;&amp; 3 &gt; 0"


def test_send_html_formats_message_before_send(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        url="https://api.telegram.org/bot-TOKEN-/sendMessage",
        method="POST",
        json={"ok": True, "result": {"message_id": 1}},
    )
    s = TelegramSender(bot_token="-TOKEN-", chat_id="12345")
    s.send("### 标题\n- **词** | a", parse_mode="HTML")
    req = httpx_mock.get_request()
    body = req.read().decode()
    assert "parse_mode=HTML" in body
    # <b>标题</b>\n• <b>词</b> | a  (urlencoded)
    assert "%3Cb%3E%E6%A0%87%E9%A2%98%3C%2Fb%3E" in body
    assert "%7C" in body  # pipe passes through as %7C (url-encoded, not escaped)


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
