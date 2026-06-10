import json

from pytest_httpx import HTTPXMock

from chat_daily_tg.config import RawChannel
from chat_daily_tg.raw_channels import (
    build_card,
    strip_promo_lines,
    strip_promo_lines_html,
    visible_text,
)
from chat_daily_tg.tg_sender import TelegramSender


def test_strip_promo_lines_html_keeps_source_link_drops_promo_footer():
    # Real 在花频道 shape: bold title, body, source <a>, promo footer <a> line.
    html = (
        "<strong>部分中国银行可提高企业美元存款利率</strong>\n\n"
        "正文一段。\n\n"
        '<a href="https://www.bloomberg.com/x">Bloomberg</a>\n\n'
        '🌸 <a href="http://t.me/ZaiHuaPd">在花频道</a> · '
        '<a href="https://t.me/zaihuatg">备用频道</a> · '
        '<a href="http://t.me/ZaiHuabot">投稿通道</a>'
    )
    out = strip_promo_lines_html(html, ["在花频道", "备用频道", "投稿通道", "投稿频道"])
    assert 'href="https://www.bloomberg.com/x">Bloomberg</a>' in out  # source link kept
    assert "ZaiHuaPd" not in out and "zaihuatg" not in out  # promo links gone
    assert "在花频道" not in out and "投稿" not in out
    assert "<strong>部分中国银行" in out  # bold title kept


def test_visible_text_strips_tags_and_unescapes():
    assert visible_text('<a href="u">A&amp;B</a>') == "A&B"


def test_strip_promo_lines_removes_footer_keeps_body():
    text = "纽约州议会通过法案\n\n正文内容一段\n\nCBS\n\n🌸 在花频道 · 备用频道 · 投稿通道"
    out = strip_promo_lines(text, ["在花频道", "备用频道", "投稿通道", "投稿频道"])
    assert "在花频道" not in out and "投稿" not in out
    assert "纽约州议会通过法案" in out and "正文内容一段" in out and "CBS" in out
    assert not out.endswith("\n")  # trailing blank collapsed


def test_strip_promo_lines_noop_without_patterns():
    text = "a\n在花频道 · 投稿通道"
    assert strip_promo_lines(text, []) == text


def test_strip_promo_lines_promo_only_becomes_empty():
    assert strip_promo_lines("🌸 在花频道 · 备用频道 · 投稿通道", ["在花频道"]) == ""


def _row(content="", msg_id=42, raw_json=""):
    return {
        "content": content,
        "msg_id": msg_id,
        "timestamp": "2026-06-05T01:30:00+00:00",  # 09:30 Asia/Shanghai
        "raw_json": raw_json,
    }


def test_build_card_public_has_link_and_escapes():
    ch = RawChannel(id="-100123", name="投机之路", username="journey_of_someone")
    card = build_card(_row(content="买 <BTC> & 卖"), ch)
    assert card is not None
    assert card.link == "https://t.me/journey_of_someone/42"
    assert "📢 <b>投机之路</b> · 09:30" in card.text_html
    assert "买 &lt;BTC&gt; &amp; 卖" in card.text_html  # content HTML-escaped


def test_build_card_private_no_link():
    ch = RawChannel(id="-100123", name="哀酱的探险小分队")  # no username
    card = build_card(_row(content="探险记录"), ch)
    assert card is not None
    assert card.link is None
    assert "探险记录" in card.text_html


def test_build_card_public_media_only_uses_placeholder():
    ch = RawChannel(id="-100123", name="美女鉴赏社", username="LamIsRealGoat")
    card = build_card(_row(content=""), ch)
    assert card is not None  # public media-only still pushed (preview shows media)
    assert card.link == "https://t.me/LamIsRealGoat/42"
    assert "媒体" in card.text_html


def test_build_card_private_media_only_skipped():
    ch = RawChannel(id="-100123", name="私有频道")
    assert build_card(_row(content=""), ch) is None


def test_build_card_forward_marker():
    ch = RawChannel(id="-100123", name="X", username="x")
    card = build_card(_row(content="hi", raw_json='{"fwd_from": 1}'), ch)
    assert "[转发]" in card.text_html


def test_send_card_public_sets_preview_no_button(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        url="https://api.telegram.org/bot-TOKEN-/sendMessage",
        method="POST",
        json={"ok": True, "result": {"message_id": 7}},
    )
    s = TelegramSender(bot_token="-TOKEN-", chat_id="12345")
    ids = s.send_card("📢 <b>x</b>\n\nhi", link="https://t.me/x/1")
    assert ids == [7]
    payload = json.loads(httpx_mock.get_request().read().decode())
    assert payload["parse_mode"] == "HTML"
    assert payload["link_preview_options"]["url"] == "https://t.me/x/1"
    assert "reply_markup" not in payload  # 打开原文按钮已移除，只留预览卡


def test_send_card_private_disables_preview(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        url="https://api.telegram.org/bot-TOKEN-/sendMessage",
        method="POST",
        json={"ok": True, "result": {"message_id": 8}},
    )
    s = TelegramSender(bot_token="-TOKEN-", chat_id="12345")
    s.send_card("📢 <b>x</b>\n\nhi", link=None)
    payload = json.loads(httpx_mock.get_request().read().decode())
    assert payload["link_preview_options"] == {"is_disabled": True}
    assert "reply_markup" not in payload


def test_send_card_degrades_to_plain_on_400(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        url="https://api.telegram.org/bot-TOKEN-/sendMessage",
        method="POST", status_code=400, json={"ok": False, "description": "bad html"},
    )
    httpx_mock.add_response(
        url="https://api.telegram.org/bot-TOKEN-/sendMessage",
        method="POST", json={"ok": True, "result": {"message_id": 9}},
    )
    s = TelegramSender(bot_token="-TOKEN-", chat_id="12345")
    ids = s.send_card("📢 <b>x</b>\n\nhi", link=None)
    assert ids == [9]
    second = json.loads(httpx_mock.get_requests()[1].read().decode())
    assert "parse_mode" not in second
    assert second["text"] == "📢 x\n\nhi"  # tags stripped
