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
    # Real 示例频道 shape: bold title, body, source <a>, promo footer <a> line.
    html = (
        "<strong>部分中国银行可提高企业美元存款利率</strong>\n\n"
        "正文一段。\n\n"
        '<a href="https://www.bloomberg.com/x">Bloomberg</a>\n\n'
        '🌸 <a href="http://t.me/ExamplePd">示例频道</a> · '
        '<a href="https://t.me/exampletg">备用频道</a> · '
        '<a href="http://t.me/ExampleBot">投稿通道</a>'
    )
    out = strip_promo_lines_html(html, ["示例频道", "备用频道", "投稿通道", "投稿频道"])
    assert 'href="https://www.bloomberg.com/x">Bloomberg</a>' in out  # source link kept
    assert "ExamplePd" not in out and "exampletg" not in out  # promo links gone
    assert "示例频道" not in out and "投稿" not in out
    assert "<strong>部分中国银行" in out  # bold title kept


def test_visible_text_strips_tags_and_unescapes():
    assert visible_text('<a href="u">A&amp;B</a>') == "A&B"


def test_strip_promo_lines_removes_footer_keeps_body():
    text = "纽约州议会通过法案\n\n正文内容一段\n\nCBS\n\n🌸 示例频道 · 备用频道 · 投稿通道"
    out = strip_promo_lines(text, ["示例频道", "备用频道", "投稿通道", "投稿频道"])
    assert "示例频道" not in out and "投稿" not in out
    assert "纽约州议会通过法案" in out and "正文内容一段" in out and "CBS" in out
    assert not out.endswith("\n")  # trailing blank collapsed


def test_strip_promo_lines_noop_without_patterns():
    text = "a\n示例频道 · 投稿通道"
    assert strip_promo_lines(text, []) == text


def test_strip_promo_lines_promo_only_becomes_empty():
    assert strip_promo_lines("🌸 示例频道 · 备用频道 · 投稿通道", ["示例频道"]) == ""


def _row(content="", msg_id=42, raw_json="", timestamp="2026-06-05T01:30:00+00:00"):
    return {
        "content": content,
        "msg_id": msg_id,
        "timestamp": timestamp,  # default 09:30 Asia/Shanghai
        "raw_json": raw_json,
    }


def test_build_card_public_has_link_and_escapes():
    ch = RawChannel(id="-100123", name="示例频道A", username="sample_channel_a")
    card = build_card(_row(content="买 <BTC> & 卖"), ch)
    assert card is not None
    assert card.link == "https://t.me/sample_channel_a/42"
    assert "📢 <b>示例频道A</b> · 09:30" in card.text_html
    assert "买 &lt;BTC&gt; &amp; 卖" in card.text_html  # content HTML-escaped


def test_build_card_private_no_link():
    ch = RawChannel(id="-100123", name="示例私有频道A")  # no username
    card = build_card(_row(content="频道正文一段"), ch)
    assert card is not None
    assert card.link is None
    assert "频道正文一段" in card.text_html


def test_build_card_public_media_only_uses_placeholder():
    ch = RawChannel(id="-100123", name="示例频道C", username="sample_channel_c")
    card = build_card(_row(content=""), ch)
    assert card is not None  # public media-only still pushed (preview shows media)
    assert card.link == "https://t.me/sample_channel_c/42"
    assert "媒体" in card.text_html


def test_build_card_private_media_only_skipped():
    ch = RawChannel(id="-100123", name="私有频道")
    assert build_card(_row(content=""), ch) is None


def test_build_card_forward_marker():
    ch = RawChannel(id="-100123", name="X", username="x")
    card = build_card(_row(content="hi", raw_json='{"fwd_from": 1}'), ch)
    assert "[转发]" in card.text_html


def test_build_card_prefer_content_link_previews_external_url():
    # repost-channel shape: body is a bare external URL → preview THAT link, not the t.me card.
    ch = RawChannel(id="-100123", name="example", username="examplechan", prefer_content_link=True)
    card = build_card(_row(content="https://github.com/foo/bar", msg_id=13523), ch)
    assert card.link == "https://github.com/foo/bar"  # preview target = content URL
    assert 'href="https://t.me/examplechan/13523">原文↗</a>' in card.text_html  # permalink kept


def test_build_card_prefer_content_link_extracts_url_among_text():
    ch = RawChannel(id="-100123", name="example", username="examplechan", prefer_content_link=True)
    card = build_card(_row(content="https://www.usenix.org/x.pdf 经典论文了", msg_id=1), ch)
    assert card.link == "https://www.usenix.org/x.pdf"


def test_build_card_prefer_content_link_strips_trailing_punct():
    ch = RawChannel(id="-100123", name="example", username="examplechan", prefer_content_link=True)
    card = build_card(_row(content="see https://x.com/a/status/1.", msg_id=2), ch)
    assert card.link == "https://x.com/a/status/1"


def test_build_card_prefer_content_link_no_url_falls_back_to_permalink():
    ch = RawChannel(id="-100123", name="example", username="examplechan", prefer_content_link=True)
    card = build_card(_row(content="上海的朋友可以去玩", msg_id=9), ch)
    assert card.link == "https://t.me/examplechan/9"  # no external URL → t.me preview
    assert "原文↗" not in card.text_html


def test_build_card_prefer_content_link_off_keeps_permalink_preview():
    # Default (other channels) unchanged: even a URL-only body previews the t.me permalink.
    ch = RawChannel(id="-100123", name="other", username="other")
    card = build_card(_row(content="https://github.com/foo/bar", msg_id=5), ch)
    assert card.link == "https://t.me/other/5"
    assert "原文↗" not in card.text_html


def test_group_albums_folds_caption_plus_media_into_one_post():
    # album shape: a caption msg + 4 media-only siblings, consecutive ids, same
    # second. One Telegram album = one post; must not render as 5 cards.
    from chat_daily_tg.raw_channels import _group_albums

    rows = [
        _row(content="相册说明", msg_id=13545),
        _row(content="", msg_id=13546),
        _row(content="", msg_id=13547),
        _row(content="", msg_id=13548),
        _row(content="", msg_id=13549, timestamp="2026-06-05T01:30:01+00:00"),  # +1s drift
    ]
    groups = _group_albums(rows)
    assert len(groups) == 1
    assert [r["msg_id"] for r in groups[0]] == [13545, 13546, 13547, 13548, 13549]


def test_group_albums_separate_text_posts_not_merged():
    # Two real posts (both have text) are never folded, even with consecutive ids.
    from chat_daily_tg.raw_channels import _group_albums

    groups = _group_albums([_row(content="第一条", msg_id=1), _row(content="第二条", msg_id=2)])
    assert len(groups) == 2


def test_group_albums_media_post_outside_window_is_own_post():
    # Consecutive id but an hour apart → a standalone photo, not an album sibling.
    from chat_daily_tg.raw_channels import _group_albums

    rows = [
        _row(content="正文", msg_id=10, timestamp="2026-06-05T01:30:00+00:00"),
        _row(content="", msg_id=11, timestamp="2026-06-05T02:30:00+00:00"),
    ]
    assert len(_group_albums(rows)) == 2


def test_group_albums_media_only_album_folds_to_single_placeholder():
    # Pure-photo album (no caption): still one card, not three.
    from chat_daily_tg.raw_channels import _group_albums

    groups = _group_albums([_row(content="", msg_id=20), _row(content="", msg_id=21),
                            _row(content="", msg_id=22)])
    assert len(groups) == 1
    assert [r["msg_id"] for r in groups[0]] == [20, 21, 22]


def test_push_album_pushes_one_card_and_marks_all_ids_seen(tmp_path, monkeypatch):
    from chat_daily_tg import raw_channels
    from chat_daily_tg.raw_seen import SeenStore

    ch = RawChannel(id="-100123", name="example", username="examplechan")
    rows = [
        _row(content="相册说明", msg_id=13545),
        _row(content="", msg_id=13546),
        _row(content="", msg_id=13547),
        _row(content="", msg_id=13548),
        _row(content="", msg_id=13549),
    ]
    monkeypatch.setattr(raw_channels, "sync_chat", lambda *a, **k: None)
    monkeypatch.setattr(raw_channels, "read_messages", lambda **k: list(rows))

    class FakeSender:
        def __init__(self):
            self.sent = []

        def send_card(self, text_html, link=None):
            self.sent.append((text_html, link))
            return [1]

    seen_path = tmp_path / "seen.txt"
    sender = FakeSender()
    kwargs = dict(channels=[ch], since="2026-06-22", until="2026-06-24",
                  db_path=tmp_path / "x.db", archive_dir=tmp_path,
                  seen_path=seen_path, delay_seconds=0)
    n = raw_channels.push_raw_channel_cards(sender=sender, **kwargs)
    assert n == 1                       # one album → one card, not five
    assert len(sender.sent) == 1
    assert "相册说明" in sender.sent[0][0]
    seen = SeenStore(seen_path)
    for mid in (13545, 13546, 13547, 13548, 13549):
        assert SeenStore.key(ch.id, mid) in seen  # every item recorded, not just the head

    # Idempotent: head already seen → re-run pushes nothing.
    again = FakeSender()
    assert raw_channels.push_raw_channel_cards(sender=again, **kwargs) == 0
    assert again.sent == []


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
