import json

from pytest_httpx import HTTPXMock

from chat_daily_tg.config import RawChannel
from chat_daily_tg.raw_channels import (
    build_card,
    matches_exclude_patterns,
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


def test_whole_post_exclusion_matches_morning_tag_and_ignores_bad_regex():
    post = "今天的起床时间是--2026-07-16 06:23:33。\n\n#morning"
    assert matches_exclude_patterns(post, [r"(?m)^#morning\s*$"])
    assert not matches_exclude_patterns("一篇正常文章", [r"(?m)^#morning\s*$", "["])


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


def test_build_card_renders_inline_markdown_link():
    # An author-typed Markdown link in the body must become a real clickable <a>,
    # not the literal "[label](url)" Telegram would otherwise show (image-2 bug).
    ch = RawChannel(id="-100123", name="示例频道A", username="sample_channel_a")
    card = build_card(_row(content="我在构想 [Sneaker Web](https://sneakerweb.org/) 来做。"), ch)
    assert card is not None
    assert '<a href="https://sneakerweb.org/">Sneaker Web</a>' in card.text_html
    assert "[Sneaker Web]" not in card.text_html  # literal markdown syntax gone


def test_build_card_markdown_link_escapes_surrounding_html():
    # Text around the link is still HTML-escaped in the same pass (no injection,
    # no double-escaping of the anchor).
    ch = RawChannel(id="-100123", name="示例频道A", username="sample_channel_a")
    card = build_card(_row(content="<b> [X](https://e.com/?a=1&b=2) </b>"), ch)
    assert card is not None
    assert '<a href="https://e.com/?a=1&amp;b=2">X</a>' in card.text_html
    assert "&lt;b&gt;" in card.text_html  # surrounding <b> escaped, not treated as markup


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


# --------------------------------------------------------------------------- #
# Public-channel media-only posts: real media is downloaded via the user session
# and re-uploaded through the bot; the 🖼 placeholder card is only the fallback.


def _fake_media_dump(path_kind_by_id: dict[int, list[tuple[str, str]]]):
    """dump_messages_by_ids double: materializes one tiny file per media item in
    the caller's out_dir (mirroring the real script) and returns the manifest."""
    def fake(chat_id, msg_ids, out_dir):
        from pathlib import Path as _P
        out = _P(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        manifest = []
        for mid in msg_ids:
            media = []
            for i, (name, kind) in enumerate(path_kind_by_id.get(mid, [])):
                p = out / f"{mid}-{i}-{name}"
                p.write_bytes(b"\xff\xd8fake")
                media.append({"path": str(p), "kind": kind})
            manifest.append({"msg_id": mid, "media": media})
        return manifest
    return fake


class _MediaSender:
    """Records send_media / send_media_group / send_card calls."""

    def __init__(self):
        self.media: list[tuple[str, str, str]] = []
        self.media_groups: list[tuple[list, str]] = []
        self.cards: list[tuple[str, str | None]] = []

    def send_media(self, path, kind, caption=""):
        self.media.append((path, kind, caption))
        return 1

    def send_media_group(self, items, caption=""):
        self.media_groups.append((list(items), caption))
        return list(range(1, len(items) + 1))

    def send_card(self, text_html, link=None):
        self.cards.append((text_html, link))
        return [1]


def _push_kwargs(ch, tmp_path, sender, seen_name="seen.txt"):
    return dict(channels=[ch], since="2026-06-05", until="2026-06-06",
                db_path=tmp_path / "x.db", sender=sender, archive_dir=tmp_path,
                seen_path=tmp_path / seen_name, delay_seconds=0)


def test_public_media_only_photo_pushes_real_media_not_placeholder(tmp_path, monkeypatch):
    import chat_daily_tg.private_media as pm
    from chat_daily_tg import raw_channels
    from chat_daily_tg.raw_seen import SeenStore

    ch = RawChannel(id="-100123", name="example", username="examplechan")
    rows = [_row(content="", msg_id=4242)]
    monkeypatch.setattr(raw_channels, "sync_chat", lambda *a, **k: None)
    monkeypatch.setattr(raw_channels, "read_messages", lambda **k: list(rows))
    monkeypatch.setattr(pm, "dump_messages_by_ids",
                        _fake_media_dump({4242: [("p.jpg", "photo")]}))

    sender = _MediaSender()
    seen_path = tmp_path / "seen.txt"
    n = raw_channels.push_raw_channel_cards(**_push_kwargs(ch, tmp_path, sender))
    assert n == 1
    assert sender.cards == []                       # no 🖼 placeholder card
    assert sender.media_groups == []
    assert len(sender.media) == 1
    path, kind, caption = sender.media[0]
    assert kind == "photo"
    assert "媒体" not in caption
    assert "📢 <b>example</b>" in caption           # card header rides as caption
    assert 'href="https://t.me/examplechan/4242"' in caption  # 原文 link kept
    assert SeenStore.key(ch.id, 4242) in SeenStore(seen_path)
    assert not (tmp_path / "rawmedia-example").exists()  # downloaded files cleaned up

    # Idempotent re-run: head seen → nothing re-sent, and no re-download either.
    calls = []
    monkeypatch.setattr(pm, "dump_messages_by_ids",
                        lambda *a, **k: calls.append(a) or [])
    again = _MediaSender()
    assert raw_channels.push_raw_channel_cards(**_push_kwargs(ch, tmp_path, again)) == 0
    assert again.media == [] and again.cards == [] and calls == []


def test_public_media_only_album_pushes_one_media_group(tmp_path, monkeypatch):
    import chat_daily_tg.private_media as pm
    from chat_daily_tg import raw_channels
    from chat_daily_tg.raw_seen import SeenStore

    ch = RawChannel(id="-100123", name="example", username="examplechan")
    rows = [_row(content="", msg_id=20), _row(content="", msg_id=21),
            _row(content="", msg_id=22)]
    monkeypatch.setattr(raw_channels, "sync_chat", lambda *a, **k: None)
    monkeypatch.setattr(raw_channels, "read_messages", lambda **k: list(rows))
    monkeypatch.setattr(pm, "dump_messages_by_ids",
                        _fake_media_dump({20: [("a.jpg", "photo")],
                                          21: [("b.jpg", "photo")],
                                          22: [("c.jpg", "photo")]}))

    sender = _MediaSender()
    seen_path = tmp_path / "seen.txt"
    n = raw_channels.push_raw_channel_cards(**_push_kwargs(ch, tmp_path, sender))
    assert n == 1                                   # one album → one push, not three
    assert sender.cards == [] and sender.media == []
    assert len(sender.media_groups) == 1
    items, caption = sender.media_groups[0]
    assert len(items) == 3                          # all album photos in one group
    assert [k for _, k in items] == ["photo", "photo", "photo"]
    assert "媒体" not in caption
    for mid in (20, 21, 22):
        assert SeenStore.key(ch.id, mid) in SeenStore(seen_path)


def test_public_media_only_download_failure_falls_back_to_placeholder(tmp_path, monkeypatch):
    import chat_daily_tg.private_media as pm
    from chat_daily_tg import raw_channels
    from chat_daily_tg.raw_seen import SeenStore

    ch = RawChannel(id="-100123", name="example", username="examplechan")
    rows = [_row(content="", msg_id=4242)]
    monkeypatch.setattr(raw_channels, "sync_chat", lambda *a, **k: None)
    monkeypatch.setattr(raw_channels, "read_messages", lambda **k: list(rows))

    def boom(chat_id, msg_ids, out_dir):
        raise RuntimeError("kabi dead")

    monkeypatch.setattr(pm, "dump_messages_by_ids", boom)

    sender = _MediaSender()
    n = raw_channels.push_raw_channel_cards(**_push_kwargs(ch, tmp_path, sender))
    assert n == 1                                   # delivery preserved
    assert sender.media == [] and sender.media_groups == []
    assert len(sender.cards) == 1                   # placeholder card as before
    text, link = sender.cards[0]
    assert "媒体" in text and link == "https://t.me/examplechan/4242"
    assert SeenStore.key(ch.id, 4242) in SeenStore(tmp_path / "seen.txt")


def test_public_media_only_no_media_in_manifest_falls_back_to_placeholder(
        tmp_path, monkeypatch):
    """The message exists but carries no downloadable media (sticker, >45MB file,
    expired photo) → placeholder card, exactly the old behavior."""
    import chat_daily_tg.private_media as pm
    from chat_daily_tg import raw_channels

    ch = RawChannel(id="-100123", name="example", username="examplechan")
    rows = [_row(content="", msg_id=4242)]
    monkeypatch.setattr(raw_channels, "sync_chat", lambda *a, **k: None)
    monkeypatch.setattr(raw_channels, "read_messages", lambda **k: list(rows))
    monkeypatch.setattr(pm, "dump_messages_by_ids", _fake_media_dump({4242: []}))

    sender = _MediaSender()
    n = raw_channels.push_raw_channel_cards(**_push_kwargs(ch, tmp_path, sender))
    assert n == 1
    assert sender.media == [] and len(sender.cards) == 1
    assert "媒体" in sender.cards[0][0]


def test_public_media_post_bypasses_dedup_gates(tmp_path, monkeypatch):
    """A media post's empty body has nothing to fingerprint/embed — the gates
    must not even be consulted (same rule as the private path)."""
    import chat_daily_tg.private_media as pm
    from chat_daily_tg import raw_channels

    ch = RawChannel(id="-100123", name="example", username="examplechan")
    rows = [_row(content="", msg_id=4242)]
    monkeypatch.setattr(raw_channels, "sync_chat", lambda *a, **k: None)
    monkeypatch.setattr(raw_channels, "read_messages", lambda **k: list(rows))
    monkeypatch.setattr(pm, "dump_messages_by_ids",
                        _fake_media_dump({4242: [("p.jpg", "photo")]}))
    gate = _StubGate(assess_raises=True)

    sender = _MediaSender()
    n = raw_channels.push_raw_channel_cards(
        **_push_kwargs(ch, tmp_path, sender), topic_gate=gate)
    assert n == 1 and len(sender.media) == 1        # gate failure never blocks media
    assert gate.refs == []                          # assess never called
    assert gate.prepared == []                      # nothing to embed for an empty body


def test_public_media_only_send_media_raises_falls_back_to_placeholder(
        tmp_path, monkeypatch):
    """Media downloaded fine but the bot upload fails → placeholder card keeps
    the delivery (a pure-photo post must never be dropped silently)."""
    import chat_daily_tg.private_media as pm
    from chat_daily_tg import raw_channels
    from chat_daily_tg.raw_seen import SeenStore

    ch = RawChannel(id="-100123", name="example", username="examplechan")
    rows = [_row(content="", msg_id=4242)]
    monkeypatch.setattr(raw_channels, "sync_chat", lambda *a, **k: None)
    monkeypatch.setattr(raw_channels, "read_messages", lambda **k: list(rows))
    monkeypatch.setattr(pm, "dump_messages_by_ids",
                        _fake_media_dump({4242: [("p.jpg", "photo")]}))

    class FailingMediaSender(_MediaSender):
        def send_media(self, path, kind, caption=""):
            raise RuntimeError("bot upload failed")

    sender = FailingMediaSender()
    n = raw_channels.push_raw_channel_cards(**_push_kwargs(ch, tmp_path, sender))
    assert n == 1
    assert len(sender.cards) == 1 and "媒体" in sender.cards[0][0]
    assert SeenStore.key(ch.id, 4242) in SeenStore(tmp_path / "seen.txt")



def test_filtered_public_post_is_not_sent_and_is_marked_seen(tmp_path, monkeypatch):
    from chat_daily_tg import dedup_journal, raw_channels
    from chat_daily_tg.raw_seen import SeenStore

    ch = RawChannel(id="-1001833253016", name="yihong", username="hyi0618",
                    exclude_patterns=[r"(?m)^#morning\s*$"])
    rows = [_row(content="起床啦。\n\n#morning", msg_id=13927)]
    monkeypatch.setattr(raw_channels, "sync_chat", lambda *a, **k: None)
    monkeypatch.setattr(raw_channels, "read_messages", lambda **k: rows)
    # Exclusions journal for auditability — capture it, or every pytest run
    # appends a fake skip line to the PRODUCTION journal that feeds the
    # daily-report footer.
    monkeypatch.setattr(dedup_journal, "record", lambda entry, **k: None)

    class FakeSender:
        sent = []

        def send_card(self, text_html, link=None):
            self.sent.append((text_html, link))

    seen_path = tmp_path / "seen.txt"
    count = raw_channels.push_raw_channel_cards(
        channels=[ch], since="2026-07-16", until="2026-07-17",
        db_path=tmp_path / "db", sender=FakeSender(), archive_dir=tmp_path,
        seen_path=seen_path, delay_seconds=0,
    )
    assert count == 0
    assert FakeSender.sent == []
    assert SeenStore.key(ch.id, 13927) in SeenStore(seen_path)


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


def test_push_raw_channels_alerts_when_all_private_fail(tmp_path, monkeypatch):
    """All private channels failing together (e.g. kabi interpreter gone) must
    surface an alert instead of returning a quiet 0 (review finding #20)."""
    import chat_daily_tg.private_media as pm
    import chat_daily_tg.notifier as notifier
    from chat_daily_tg.raw_channels import push_raw_channel_cards
    from chat_daily_tg.config import RawChannel

    def boom(**kwargs):
        raise RuntimeError("kabi dead")

    monkeypatch.setattr(pm, "push_private_channel", boom)
    alerts = []
    monkeypatch.setattr(notifier, "notify_failure", lambda t, m: alerts.append((t, m)))

    chans = [RawChannel(id="-100a", name="A"), RawChannel(id="-100b", name="B")]  # private
    n = push_raw_channel_cards(
        channels=chans, since="2026-06-05", until="2026-06-06",
        db_path=tmp_path / "tg.db", sender=None, archive_dir=tmp_path / "arch",
        seen_path=tmp_path / "seen.txt", sync_before_export=False, delay_seconds=0,
        no_push=False, incremental=True,
    )
    assert n == 0
    assert len(alerts) == 1
    assert "私有频道全部失败" in alerts[0][0]


# --------------------------------------------------------------------------- #
# Dedup-layer wiring integration tests (appended): push_raw_channel_cards with
# content_store= (L1), topic_gate= (L2) and the resend_raw_card escape hatch.
# Everything goes through the public push_raw_channel_cards API; the L2 gate is
# a stub here (TopicDedupGate has its own unit tests in test_topic_dedup.py).

from types import SimpleNamespace

from chat_daily_tg.config import RawChannel as _RC  # noqa: F401 (clarity alias)
from chat_daily_tg.content_seen import ContentSeenStore, fingerprints_for
from chat_daily_tg.raw_seen import SeenStore

# ≥24 substantive code points after normalization → gets a text fingerprint.
_DUP_TEXT = (
    "纽约州议会周三通过一项校园食品安全法案，"
    "要求全州公立学校每个季度公开披露餐饮采购来源与供应商资质审查结果。"
)
_SHARED_URL = "https://example.com/2026/dist-systems-consistency"


class _RecordingSender:
    """Fake sender matching the REAL TelegramSender.send_card return type:
    a list of the Telegram message ids of the sent chunks. chat_id matches the
    stub gate's group so the forum-guard in _l2_register lets registration
    through (a DM-fallback sender would be filtered — tested separately)."""

    def __init__(self, start_id: int = 100, chat_id: str = "-100424841223"):
        self.sent: list[tuple[str, str | None]] = []
        self._next = start_id
        self.chat_id = chat_id
        self.message_thread_id = 41

    def send_card(self, text_html, link=None):
        self.sent.append((text_html, link))
        mid = self._next
        self._next += 1
        return [mid]


def _capture_journal(monkeypatch):
    """Redirect dedup_journal.record so no test touches the real journal file."""
    import chat_daily_tg.dedup_journal as dj

    entries: list[dict] = []
    monkeypatch.setattr(dj, "record", lambda entry, path=None: entries.append(entry))
    return entries


def _push_channels(monkeypatch, tmp_path, *, channels, rows_by_chat, sender,
                   seen_path, **extra):
    """Drive the public API with monkeypatched sync/read (same convention as
    test_filtered_public_post_is_not_sent_and_is_marked_seen above)."""
    from chat_daily_tg import raw_channels

    monkeypatch.setattr(raw_channels, "sync_chat", lambda *a, **k: None)
    monkeypatch.setattr(
        raw_channels, "read_messages",
        lambda **k: list(rows_by_chat.get(k["chat_id"], [])),
    )
    return raw_channels.push_raw_channel_cards(
        channels=channels, since="2026-07-15", until="2026-07-16",
        db_path=tmp_path / "messages.db", sender=sender, archive_dir=tmp_path,
        seen_path=seen_path, delay_seconds=0, **extra,
    )


def test_cross_channel_text_dup_suppressed_marks_seen_and_stays_idempotent(
        tmp_path, monkeypatch):
    journal = _capture_journal(monkeypatch)
    store = ContentSeenStore(tmp_path / "cs.db")
    seen_path = tmp_path / "seen.txt"
    ch_a = RawChannel(id="-1001111", name="A频道", username="chan_a")
    ch_b = RawChannel(id="-1002222", name="B频道", username="chan_b")
    rows = {
        "-1001111": [_row(content=_DUP_TEXT, msg_id=101)],
        "-1002222": [_row(content=_DUP_TEXT, msg_id=555)],  # same body, new msg_id
    }

    sender_a = _RecordingSender()
    n = _push_channels(monkeypatch, tmp_path, channels=[ch_a], rows_by_chat=rows,
                       sender=sender_a, seen_path=seen_path, content_store=store)
    assert n == 1 and len(sender_a.sent) == 1
    hit = store.lookup(fingerprints_for(_DUP_TEXT))  # write-after-send registered
    assert hit is not None
    assert hit.chat_id == "-1001111" and hit.msg_id == 101

    sender_b = _RecordingSender()
    n = _push_channels(monkeypatch, tmp_path, channels=[ch_b], rows_by_chat=rows,
                       sender=sender_b, seen_path=seen_path, content_store=store)
    assert n == 0 and sender_b.sent == []          # duplicate suppressed
    seen = SeenStore(seen_path)
    assert SeenStore.key(ch_b.id, 555) in seen      # high-water mark advances
    assert seen.max_msg_id(ch_b.id) == 555

    # Re-running the same push sends nothing: the head id is already seen.
    sender_b2 = _RecordingSender()
    n = _push_channels(monkeypatch, tmp_path, channels=[ch_b], rows_by_chat=rows,
                       sender=sender_b2, seen_path=seen_path, content_store=store)
    assert n == 0 and sender_b2.sent == []
    assert len(journal) == 1                        # skip journaled exactly once


def test_bare_link_url_dup_suppressed_but_commentary_delivers(tmp_path, monkeypatch):
    _capture_journal(monkeypatch)
    store = ContentSeenStore(tmp_path / "cs.db")
    seen_path = tmp_path / "seen.txt"
    ch_a = RawChannel(id="-1001111", name="A频道", username="chan_a")
    ch_b = RawChannel(id="-1002222", name="B频道", username="chan_b")
    ch_c = RawChannel(id="-1003333", name="C频道", username="chan_c")
    text_a = ("这篇讲分布式系统一致性取舍的长文非常扎实，"
              f"从线性一致到最终一致的推导都配了工程实例，推荐后端工程师精读。 {_SHARED_URL}")
    text_b = _SHARED_URL                                   # bare link, no commentary
    text_c = f"补充一个不同视角的评论，观点与上文相反，值得对照阅读。 {_SHARED_URL}"
    rows = {
        "-1001111": [_row(content=text_a, msg_id=11)],
        "-1002222": [_row(content=text_b, msg_id=22)],
        "-1003333": [_row(content=text_c, msg_id=33)],
    }

    n = _push_channels(monkeypatch, tmp_path, channels=[ch_a], rows_by_chat=rows,
                       sender=_RecordingSender(), seen_path=seen_path,
                       content_store=store)
    assert n == 1                                          # commentary+URL delivered

    sender_b = _RecordingSender()
    n = _push_channels(monkeypatch, tmp_path, channels=[ch_b], rows_by_chat=rows,
                       sender=sender_b, seen_path=seen_path, content_store=store)
    assert n == 0 and sender_b.sent == []                  # bare re-link suppressed
    assert SeenStore.key(ch_b.id, 22) in SeenStore(seen_path)

    sender_c = _RecordingSender()
    n = _push_channels(monkeypatch, tmp_path, channels=[ch_c], rows_by_chat=rows,
                       sender=sender_c, seen_path=seen_path, content_store=store)
    assert n == 1 and len(sender_c.sent) == 1              # own commentary delivers
    assert "补充一个不同视角的评论" in sender_c.sent[0][0]


def test_per_channel_dedup_opt_out_delivers_despite_fingerprint_hit(
        tmp_path, monkeypatch):
    journal = _capture_journal(monkeypatch)
    store = ContentSeenStore(tmp_path / "cs.db")
    store.register(fingerprints_for(_DUP_TEXT), "-1001111", 101, "A频道")
    ch_b = RawChannel(id="-1002222", name="B频道", username="chan_b", dedup=False)
    rows = {"-1002222": [_row(content=_DUP_TEXT, msg_id=555)]}

    sender = _RecordingSender()
    n = _push_channels(monkeypatch, tmp_path, channels=[ch_b], rows_by_chat=rows,
                       sender=sender, seen_path=tmp_path / "seen.txt",
                       content_store=store)
    assert n == 1 and len(sender.sent) == 1     # dedup: false wins over the hit
    assert _DUP_TEXT in sender.sent[0][0]
    assert journal == []                        # no suppression, nothing journaled


def test_content_store_failure_still_delivers(tmp_path, monkeypatch):
    class BoomStore:
        def lookup(self, fingerprints):
            raise RuntimeError("content_seen db locked")

        def register(self, *a, **k):
            raise RuntimeError("content_seen db locked")

    ch = RawChannel(id="-1001111", name="A频道", username="chan_a")
    rows = {"-1001111": [_row(content=_DUP_TEXT, msg_id=101)]}
    sender = _RecordingSender()
    seen_path = tmp_path / "seen.txt"
    n = _push_channels(monkeypatch, tmp_path, channels=[ch], rows_by_chat=rows,
                       sender=sender, seen_path=seen_path, content_store=BoomStore())
    assert n == 1 and len(sender.sent) == 1     # 投递优先于完美
    assert SeenStore.key(ch.id, 101) in SeenStore(seen_path)


def test_l1_skip_writes_one_journal_entry(tmp_path, monkeypatch):
    entries = _capture_journal(monkeypatch)
    store = ContentSeenStore(tmp_path / "cs.db")
    store.register(fingerprints_for(_DUP_TEXT), "-1001111", 101, "A频道")
    ch_b = RawChannel(id="-1002222", name="B频道", username="chan_b")
    rows = {"-1002222": [_row(content=_DUP_TEXT, msg_id=555)]}

    n = _push_channels(monkeypatch, tmp_path, channels=[ch_b], rows_by_chat=rows,
                       sender=_RecordingSender(), seen_path=tmp_path / "seen.txt",
                       content_store=store)
    assert n == 0
    assert len(entries) == 1
    e = entries[0]
    assert e["layer"] == "L1" and e["action"] == "skip" and e["reason"] == "text"
    assert e["chat_id"] == "-1002222" and e["msg_id"] == 555
    assert e["matched_msg_id"] == 101 and e["matched_channel"] == "A频道"


def test_no_content_store_keeps_existing_behavior_duplicates_send(
        tmp_path, monkeypatch):
    # content_store omitted (default None) → the dedup layers are inert and a
    # cross-channel duplicate is delivered twice, byte-identical to old behavior.
    seen_path = tmp_path / "seen.txt"
    ch_a = RawChannel(id="-1001111", name="A频道", username="chan_a")
    ch_b = RawChannel(id="-1002222", name="B频道", username="chan_b")
    rows = {
        "-1001111": [_row(content=_DUP_TEXT, msg_id=101)],
        "-1002222": [_row(content=_DUP_TEXT, msg_id=555)],
    }
    sender = _RecordingSender()
    n = _push_channels(monkeypatch, tmp_path, channels=[ch_a], rows_by_chat=rows,
                       sender=sender, seen_path=seen_path)
    n += _push_channels(monkeypatch, tmp_path, channels=[ch_b], rows_by_chat=rows,
                        sender=sender, seen_path=seen_path)
    assert n == 2 and len(sender.sent) == 2
    assert _DUP_TEXT in sender.sent[0][0] and _DUP_TEXT in sender.sent[1][0]


def test_resend_raw_card_bridges_bare_positive_chat_id_and_marks_seen(tmp_path):
    # resend queries messages.db directly; the row's chat_id is stored in the
    # bare-positive tg-cli form while channel.id is the "-100…" config form —
    # canonical_chat_ids must bridge the two.
    import sqlite3

    from chat_daily_tg.raw_channels import resend_raw_card

    db = tmp_path / "messages.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE messages (chat_id INTEGER, chat_name TEXT, msg_id INTEGER, "
        "sender_name TEXT, content TEXT, timestamp TEXT, raw_json TEXT)"
    )
    conn.execute(
        "INSERT INTO messages VALUES (?,?,?,?,?,?,?)",
        (1833253016, "yihong", 13927, "y", "误杀后需要补发的正文一段",
         "2026-07-15T02:00:00+00:00", None),
    )
    conn.commit()
    conn.close()

    ch = RawChannel(id="-1001833253016", name="yihong", username="hyi0618")
    sender = _RecordingSender()
    seen_path = tmp_path / "seen.txt"
    ok = resend_raw_card(channel=ch, msg_id=13927, db_path=db,
                         sender=sender, seen_path=seen_path)
    assert ok is True
    assert len(sender.sent) == 1
    text, link = sender.sent[0]
    assert "误杀后需要补发的正文一段" in text
    assert link == "https://t.me/hyi0618/13927"
    assert SeenStore.key(ch.id, 13927) in SeenStore(seen_path)


def test_resend_raw_card_missing_msg_returns_false(tmp_path):
    import sqlite3

    from chat_daily_tg.raw_channels import resend_raw_card

    db = tmp_path / "messages.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE messages (chat_id INTEGER, chat_name TEXT, msg_id INTEGER, "
        "sender_name TEXT, content TEXT, timestamp TEXT, raw_json TEXT)"
    )
    conn.commit()
    conn.close()
    ch = RawChannel(id="-1001833253016", name="yihong", username="hyi0618")
    sender = _RecordingSender()
    assert resend_raw_card(channel=ch, msg_id=1, db_path=db, sender=sender,
                           seen_path=tmp_path / "seen.txt") is False
    assert sender.sent == []


# ---- L2 gate wiring (stub gate — the real TopicDedupGate has its own tests) --


class _StubIndex:
    def __init__(self):
        self.registered: list[dict] = []

    def register_sent(self, msg_ids, text, producer, thread_id=None, vector=None):
        self.registered.append({
            "msg_ids": msg_ids, "text": text, "producer": producer,
            "thread_id": thread_id, "vector": vector,
        })


class _StubGate:
    """Only the surface the send paths touch: prepare / assess(ref=) /
    annotation_html / register_sent / group_internal_id (forum guard)."""

    def __init__(self, verdict=None, assess_raises=False):
        self.index = _StubIndex()
        self.prepared: list[list[str]] = []
        self.refs: list[dict | None] = []
        self.group_internal_id = "424841223"
        self._verdict = verdict
        self._raises = assess_raises

    def prepare(self, texts):
        self.prepared.append(list(texts))

    def assess(self, text, ref=None):
        self.refs.append(ref)
        if self._raises:
            raise RuntimeError("embedder down")
        return self._verdict

    def register_sent(self, msg_ids, text, producer, thread_id=None, vector=None):
        self.index.register_sent(msg_ids, text, producer, thread_id, vector)

    def annotation_html(self, matched_msg_id):
        return ("🔁 疑似同一事件 · "
                f'<a href="https://t.me/c/424841223/{matched_msg_id}">前文↗</a>')


def test_topic_gate_skip_suppresses_card_and_marks_seen(tmp_path, monkeypatch):
    gate = _StubGate(verdict=SimpleNamespace(
        action="skip", matched_msg_id=777, similarity=0.95,
        vector=[0.1, 0.2], new_info="none"))
    ch = RawChannel(id="-1001111", name="A频道", username="chan_a")
    rows = {"-1001111": [_row(content=_DUP_TEXT, msg_id=101)]}
    sender = _RecordingSender()
    seen_path = tmp_path / "seen.txt"
    n = _push_channels(monkeypatch, tmp_path, channels=[ch], rows_by_chat=rows,
                       sender=sender, seen_path=seen_path, topic_gate=gate)
    assert n == 0 and sender.sent == []
    assert SeenStore.key(ch.id, 101) in SeenStore(seen_path)  # terminal, hwm moves
    assert gate.prepared == [[_DUP_TEXT]]       # one embed batch per channel
    assert gate.index.registered == []          # nothing sent → nothing indexed


def test_topic_gate_annotate_prepends_annotation_and_registers_sent(
        tmp_path, monkeypatch):
    verdict = SimpleNamespace(action="annotate", matched_msg_id=777,
                              similarity=0.91, vector=[0.3, 0.4], new_info="minor")
    gate = _StubGate(verdict=verdict)
    ch = RawChannel(id="-1001111", name="A频道", username="chan_a")
    rows = {"-1001111": [_row(content=_DUP_TEXT, msg_id=101)]}
    sender = _RecordingSender()
    n = _push_channels(monkeypatch, tmp_path, channels=[ch], rows_by_chat=rows,
                       sender=sender, seen_path=tmp_path / "seen.txt",
                       topic_gate=gate)
    assert n == 1 and len(sender.sent) == 1
    text = sender.sent[0][0]
    assert "🔁 疑似同一事件" in text and "t.me/c/424841223/777" in text
    assert _DUP_TEXT in text                    # original body intact
    assert text.startswith("📢 ")               # header still leads the card
    assert len(gate.index.registered) == 1
    reg = gate.index.registered[0]
    assert reg["msg_ids"] == [100]              # the sender-returned message ids
    assert reg["text"] == _DUP_TEXT
    assert reg["producer"] == "chatdaily_raw"
    assert reg["vector"] == [0.3, 0.4]          # verdict vector reused, no re-embed


def test_topic_gate_assess_failure_delivers_unmodified(tmp_path, monkeypatch):
    gate = _StubGate(assess_raises=True)
    ch = RawChannel(id="-1001111", name="A频道", username="chan_a")
    rows = {"-1001111": [_row(content=_DUP_TEXT, msg_id=101)]}
    sender = _RecordingSender()
    seen_path = tmp_path / "seen.txt"
    n = _push_channels(monkeypatch, tmp_path, channels=[ch], rows_by_chat=rows,
                       sender=sender, seen_path=seen_path, topic_gate=gate)
    assert n == 1 and len(sender.sent) == 1     # gate failure never blocks delivery
    text = sender.sent[0][0]
    assert "疑似同一事件" not in text and _DUP_TEXT in text
    assert SeenStore.key(ch.id, 101) in SeenStore(seen_path)
    assert len(gate.index.registered) == 1      # still indexed for future runs
    assert gate.index.registered[0]["vector"] is None  # no verdict → no vector


def test_l2_register_forum_guard_blocks_dm_fallback_allows_forum(tmp_path, monkeypatch):
    """resolve_tg_target can fall back to the DM on a missing topic key; DM
    message ids live in a different id-space, so a delivered card must NOT be
    registered into the forum index — but the delivery itself is unaffected.
    The same card sent by a real forum sender IS registered."""
    ch = RawChannel(id="-1001111", name="A频道", username="chan_a")
    rows = {"-1001111": [_row(content=_DUP_TEXT, msg_id=101)]}
    deliver = SimpleNamespace(action="deliver", matched_msg_id=None,
                              similarity=0.0, vector=[0.1, 0.2],
                              new_info="substantial")

    dm_gate = _StubGate(verdict=deliver)
    dm_sender = _RecordingSender(chat_id="999888777")  # DM, not the forum
    n = _push_channels(monkeypatch, tmp_path, channels=[ch], rows_by_chat=rows,
                       sender=dm_sender, seen_path=tmp_path / "seen-dm.txt",
                       topic_gate=dm_gate)
    assert n == 1 and len(dm_sender.sent) == 1  # delivered normally
    assert dm_gate.index.registered == []       # DM ids never enter the index

    forum_gate = _StubGate(verdict=deliver)
    forum_sender = _RecordingSender(chat_id="-100424841223")  # matches the gate
    n = _push_channels(monkeypatch, tmp_path, channels=[ch], rows_by_chat=rows,
                       sender=forum_sender, seen_path=tmp_path / "seen-forum.txt",
                       topic_gate=forum_gate)
    assert n == 1
    assert len(forum_gate.index.registered) == 1
    assert forum_gate.index.registered[0]["msg_ids"] == [100]


def test_exclude_pattern_suppression_writes_l1_journal_entry(tmp_path, monkeypatch):
    """An exclude_patterns hit is a terminal suppression that never reaches the
    rawcard archive — the journal entry is its only trace, and --resend's
    documented recovery flow starts from it."""
    entries = _capture_journal(monkeypatch)
    ch = RawChannel(id="-1002222", name="B频道", username="chan_b",
                    exclude_patterns=[r"(?m)^#morning\s*$"])
    rows = {"-1002222": [_row(content="起床啦。\n\n#morning", msg_id=777)]}
    sender = _RecordingSender()
    n = _push_channels(monkeypatch, tmp_path, channels=[ch], rows_by_chat=rows,
                       sender=sender, seen_path=tmp_path / "seen.txt")
    assert n == 0 and sender.sent == []
    assert len(entries) == 1
    e = entries[0]
    assert e["layer"] == "L1"
    assert e["action"] == "skip"
    assert e["reason"] == "exclude_pattern"
    assert e["chat_id"] == "-1002222"
    assert e["msg_id"] == 777
    assert e["channel"] == "B频道"
    assert SeenStore.key(ch.id, 777) in SeenStore(tmp_path / "seen.txt")
