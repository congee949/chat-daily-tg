from pytest_httpx import HTTPXMock
import json
import pytest
import httpx
from unittest.mock import patch
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


# --- LNK-1: anchor links from post_process must survive format_html (no re-escape) ---

def test_format_html_preserves_anchor_links():
    inp = '查看<a href="https://e.com/a?x=1&amp;y=2">活动</a>了解'
    out = format_html_for_telegram(inp)
    assert '<a href="https://e.com/a?x=1&amp;y=2">活动</a>' in out
    assert "&lt;a" not in out          # tag not escaped to literal text
    assert "&amp;amp;" not in out      # existing &amp; not double-escaped


def test_link_round_trip_post_process_then_html():
    from chat_daily_tg.post_process import post_process_concise
    md = "查看[活动链接](https://example.com/a?x=1&y=2)了解详情"
    out = format_html_for_telegram(post_process_concise(md, {}))
    assert '<a href="https://example.com/a?x=1&amp;y=2">活动链接</a>' in out
    assert "&lt;a" not in out


def test_format_html_link_and_bold_together():
    inp = '**重点**：见<a href="https://x.com">链接</a>'
    out = format_html_for_telegram(inp)
    assert "<b>重点</b>" in out
    assert '<a href="https://x.com">链接</a>' in out


# --- CHUNK-1: chunking happens AFTER formatting, so HTML expansion can't overflow 4096 ---

@pytest.mark.httpx_mock(can_send_already_matched_responses=True)
def test_send_html_splits_after_formatting_expansion(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        url="https://api.telegram.org/bot-TOKEN-/sendMessage",
        method="POST",
        json={"ok": True, "result": {"message_id": 1}},
    )
    s = TelegramSender(bot_token="-TOKEN-", chat_id="12345")
    # raw ~3030 chars (1 chunk if split on raw), but '<'->'&lt;' expands ~4x to ~12000
    text = "\n".join(["<" * 100 for _ in range(30)])
    assert len(text) < 3900   # would be a single chunk under the old raw-length split
    s.send(text, parse_mode="HTML")
    reqs = httpx_mock.get_requests()
    assert len(reqs) >= 2     # now split post-format, so multiple sends


# --- image output: send_photo posts multipart to sendPhoto and caps caption ---

def test_send_photo_posts_multipart(httpx_mock: HTTPXMock, tmp_path):
    httpx_mock.add_response(
        url="https://api.telegram.org/bot-TOKEN-/sendPhoto",
        method="POST",
        json={"ok": True, "result": {"message_id": 7}},
    )
    png = tmp_path / "card.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\nfakecontent")
    s = TelegramSender(bot_token="-TOKEN-", chat_id="12345")
    mid = s.send_photo(png, caption="hi there")
    assert mid == 7
    req = httpx_mock.get_request()
    assert req.url.path.endswith("/sendPhoto")
    body = req.read()
    assert b"12345" in body and b"hi there" in body


def test_send_photo_omits_empty_caption(httpx_mock: HTTPXMock, tmp_path):
    httpx_mock.add_response(
        url="https://api.telegram.org/bot-TOKEN-/sendPhoto",
        method="POST",
        json={"ok": True, "result": {"message_id": 9}},
    )
    png = tmp_path / "card.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n")
    s = TelegramSender(bot_token="-TOKEN-", chat_id="12345")
    s.send_photo(png, caption="")
    body = httpx_mock.get_request().read().decode("utf-8", "replace")
    assert "12345" in body
    assert "caption" not in body   # pure image, no caption field


def test_send_photo_button_becomes_inline_keyboard(httpx_mock: HTTPXMock, tmp_path):
    httpx_mock.add_response(
        url="https://api.telegram.org/bot-TOKEN-/sendPhoto",
        method="POST",
        json={"ok": True, "result": {"message_id": 3}},
    )
    png = tmp_path / "card.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n")
    s = TelegramSender(bot_token="-TOKEN-", chat_id="12345")
    s.send_photo(png, caption="hi", button=("看视频", "https://example.com/v"))
    body = httpx_mock.get_request().read().decode("utf-8", "replace")
    assert "reply_markup" in body and "inline_keyboard" in body
    assert "https://example.com/v" in body


def test_send_card_button_on_last_chunk_only(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        url="https://api.telegram.org/bot-TOKEN-/sendMessage",
        method="POST",
        json={"ok": True, "result": {"message_id": 1}},
        is_reusable=True,
    )
    s = TelegramSender(bot_token="-TOKEN-", chat_id="12345")
    long_text = "\n".join(["L" * 100] * 60)  # forces >1 chunk at limit 3900
    s.send_card(long_text, button=("打开", "https://example.com/x"))
    reqs = httpx_mock.get_requests()
    assert len(reqs) > 1
    payloads = [json.loads(r.read()) for r in reqs]
    assert all("reply_markup" not in p for p in payloads[:-1])
    kb = payloads[-1]["reply_markup"]["inline_keyboard"]
    assert kb == [[{"text": "打开", "url": "https://example.com/x"}]]


def test_send_photo_truncates_caption(httpx_mock: HTTPXMock, tmp_path):
    httpx_mock.add_response(
        url="https://api.telegram.org/bot-TOKEN-/sendPhoto",
        method="POST",
        json={"ok": True, "result": {"message_id": 1}},
    )
    png = tmp_path / "card.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n")
    s = TelegramSender(bot_token="-TOKEN-", chat_id="12345")
    s.send_photo(png, caption="A" * 2000)
    body = httpx_mock.get_request().read().decode("utf-8", "replace")
    assert "A" * 1024 in body
    assert "A" * 1025 not in body


def test_send_media_keeps_html_caption_when_visible_text_fits(httpx_mock: HTTPXMock, tmp_path):
    """Telegram's 1024 caption limit counts VISIBLE chars: a caption whose markup
    pushes the raw HTML past 1024 must NOT be sliced (cutting inside the <a> tag
    would 400 on every retry)."""
    httpx_mock.add_response(
        url="https://api.telegram.org/bot-TOKEN-/sendPhoto",
        method="POST",
        json={"ok": True, "result": {"message_id": 1}},
    )
    f = tmp_path / "a.jpg"
    f.write_bytes(b"fakejpg")
    caption = f'<a href="https://example.com/{"x" * 1200}">来源</a> ' + "正" * 900
    s = TelegramSender(bot_token="-TOKEN-", chat_id="12345")
    s.send_media(str(f), "photo", caption=caption)
    body = httpx_mock.get_request().read().decode("utf-8", "replace")
    assert "</a>" in body            # tag survived intact
    assert "x" * 1200 in body        # href not sliced
    assert "HTML" in body            # parse_mode kept


def test_send_media_overlong_visible_caption_degrades_to_plain(httpx_mock: HTTPXMock, tmp_path):
    """When even the VISIBLE text exceeds 1024, degrade to truncated plain text
    without parse_mode — truncated HTML would be malformed and 400."""
    httpx_mock.add_response(
        url="https://api.telegram.org/bot-TOKEN-/sendPhoto",
        method="POST",
        json={"ok": True, "result": {"message_id": 1}},
    )
    f = tmp_path / "a.jpg"
    f.write_bytes(b"fakejpg")
    s = TelegramSender(bot_token="-TOKEN-", chat_id="12345")
    s.send_media(str(f), "photo", caption="<b>" + "Z" * 1500 + "</b>")
    body = httpx_mock.get_request().read().decode("utf-8", "replace")
    assert "Z" * 1024 in body
    assert "Z" * 1025 not in body
    assert "<b>" not in body         # tags stripped, not sliced mid-tag
    assert "parse_mode" not in body  # plain text: stray '<' must not hit the HTML parser


def test_send_rich_message_uploads_media_in_same_multipart_request(
    httpx_mock: HTTPXMock, tmp_path
):
    httpx_mock.add_response(
        url="https://api.telegram.org/bot-TOKEN-/sendRichMessage",
        method="POST",
        json={"ok": True, "result": {"message_id": 9}},
    )
    image = tmp_path / "health.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n")
    s = TelegramSender(
        bot_token="-TOKEN-", chat_id="12345", message_thread_id=77
    )

    message_id = s.send_rich_message(
        markdown='![](tg://photo?id=health_chart "健康概览")',
        media=[("health_chart", str(image), "photo")],
    )

    assert message_id == 9
    body = httpx_mock.get_request().read().decode("utf-8", "replace")
    assert "rich_message" in body
    assert "health_chart" in body
    assert "attach%3A%2F%2Frich_media_0" in body or "attach://rich_media_0" in body
    assert "message_thread_id" in body
    assert "health.png" in body


def test_send_multichunk_resumes_after_partial_failure(tmp_path, httpx_mock, mocker):
    """A 2-chunk push whose 2nd chunk fails must, on the next run, resume from the
    2nd chunk instead of re-sending the first half (review finding #42)."""
    import json as _json
    import httpx
    import pytest
    from chat_daily_tg.tg_sender import TelegramSender

    mocker.patch("chat_daily_tg.tg_sender.time.sleep")
    long_text = ("A" * 3800) + "\n" + ("B" * 3800)  # two newline-split chunks
    state = tmp_path / "push-state.json"
    s = TelegramSender(bot_token="-TOKEN-", chat_id="12345", retry_max_attempts=1)

    url = "https://api.telegram.org/bot-TOKEN-/sendMessage"
    # Run 1: chunk 1 ok, chunk 2 fails.
    httpx_mock.add_response(url=url, method="POST", json={"ok": True, "result": {"message_id": 1}})
    httpx_mock.add_response(url=url, method="POST", status_code=500, json={"ok": False})
    with pytest.raises(httpx.HTTPStatusError):
        s.send(long_text, state_path=state)
    assert _json.loads(state.read_text())["sent"] == 1  # progress recorded

    # Run 2: only the 2nd chunk should be (re)sent.
    httpx_mock.add_response(url=url, method="POST", json={"ok": True, "result": {"message_id": 2}})
    ids = s.send(long_text, state_path=state)
    assert ids == [2]
    # Total POSTs = chunk1(ok) + chunk2(fail) + chunk2(ok) = 3, NOT 4 (no first-half resend).
    assert len(httpx_mock.get_requests()) == 3


def test_sender_reuses_one_pool_and_closes_it():
    with patch("chat_daily_tg.tg_sender.httpx.Client") as client_cls:
        http = client_cls.return_value.__enter__.return_value
        response = http.post.return_value
        response.json.return_value = {"ok": True, "result": {"message_id": 1}}
        response.raise_for_status.return_value = None
        sender = TelegramSender(bot_token="-TOKEN-", chat_id="12345")
        sender.send("one")
        sender.send("two")
        assert client_cls.call_count == 1
        sender.close()
        client_cls.return_value.__exit__.assert_called_once()
