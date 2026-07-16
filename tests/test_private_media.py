import json

from pytest_httpx import HTTPXMock

from chat_daily_tg.private_media import group_posts
from chat_daily_tg.tg_sender import TelegramSender


def test_group_posts_standalone():
    manifest = [
        {"msg_id": 1, "date": "2026-06-05T09:21:00+08:00", "text": "a", "grouped_id": None,
         "media": [{"path": "/x/1.jpg", "kind": "photo"}]},
        {"msg_id": 2, "date": "2026-06-05T16:13:00+08:00", "text": "b", "grouped_id": None, "media": []},
    ]
    posts = group_posts(manifest)
    assert len(posts) == 2
    assert posts[0].time == "09:21"
    assert posts[0].media == [("/x/1.jpg", "photo")]
    assert posts[0].msg_ids == [1]
    assert posts[1].text == "b" and posts[1].media == []


def test_group_posts_album_merges():
    manifest = [
        {"msg_id": 10, "date": "2026-06-05T10:00:00+08:00", "text": "album cap", "grouped_id": 999,
         "media": [{"path": "/x/10.jpg", "kind": "photo"}]},
        {"msg_id": 11, "date": "2026-06-05T10:00:00+08:00", "text": "", "grouped_id": 999,
         "media": [{"path": "/x/11.jpg", "kind": "photo"}]},
        {"msg_id": 12, "date": "2026-06-05T10:00:00+08:00", "text": "", "grouped_id": 999,
         "media": [{"path": "/x/12.jpg", "kind": "photo"}]},
    ]
    posts = group_posts(manifest)
    assert len(posts) == 1
    p = posts[0]
    assert p.first_msg_id == 10
    assert p.text == "album cap"
    assert len(p.media) == 3
    assert p.msg_ids == [10, 11, 12]


def test_push_private_channel_album_advances_high_water_mark(tmp_path, monkeypatch):
    """Every album item id must land in the seen store: recording only the first
    would stall max_msg_id at the album head and the next incremental run would
    re-fetch and re-send 11/12 as a partial album."""
    from chat_daily_tg import private_media
    from chat_daily_tg.config import RawChannel
    from chat_daily_tg.raw_seen import SeenStore

    manifest = [
        {"msg_id": 10, "date": "2026-06-05T10:00:00+08:00", "text": "album cap", "grouped_id": 9,
         "media": [{"path": "/x/10.jpg", "kind": "photo"}]},
        {"msg_id": 11, "date": "2026-06-05T10:00:00+08:00", "text": "", "grouped_id": 9,
         "media": [{"path": "/x/11.jpg", "kind": "photo"}]},
        {"msg_id": 12, "date": "2026-06-05T10:00:00+08:00", "text": "", "grouped_id": 9,
         "media": [{"path": "/x/12.jpg", "kind": "photo"}]},
    ]
    monkeypatch.setattr(private_media, "dump_channel", lambda *a, **k: manifest)

    class FakeSender:
        def __init__(self):
            self.albums = []

        def send_media_group(self, items, *, caption=""):
            self.albums.append(items)
            return [1, 2, 3]

    sender = FakeSender()
    seen = SeenStore(tmp_path / "seen.txt")
    pushed = private_media.push_private_channel(
        channel=RawChannel(id="-100x", name="C"),
        since="2026-06-05", until="2026-06-06",
        out_dir=tmp_path / "dump", sender=sender, limit=500,
        seen=seen, delay_seconds=0,
    )
    assert pushed == 1
    assert len(sender.albums) == 1
    assert seen.max_msg_id("-100x") == 12  # not 10
    for mid in (10, 11, 12):
        assert SeenStore.key("-100x", mid) in seen


def test_push_private_channel_partial_media_loss_alerts_and_marks_seen(tmp_path, monkeypatch):
    """A mixed album where one item fails: the post is still marked seen (the
    high-water mark advances regardless) but the loss is alerted, not silent."""
    from chat_daily_tg import private_media
    from chat_daily_tg.config import RawChannel
    from chat_daily_tg.raw_seen import SeenStore

    manifest = [
        {"msg_id": 30, "date": "2026-06-05T10:00:00+08:00", "text": "mix", "grouped_id": 7,
         "media": [{"path": "/x/a.jpg", "kind": "photo"}, {"path": "/x/b.pdf", "kind": "document"}]},
    ]
    monkeypatch.setattr(private_media, "dump_channel", lambda *a, **k: manifest)
    alerts = []
    monkeypatch.setattr(private_media, "notify_failure", lambda t, m: alerts.append((t, m)))

    class FakeSender:
        def send_media(self, path, kind, caption=""):
            if kind == "document":
                raise RuntimeError("400 bad")
            return 1

        def send_card(self, *a, **k):
            return 1

    seen = SeenStore(tmp_path / "seen.txt")
    pushed = private_media.push_private_channel(
        channel=RawChannel(id="-100y", name="C"), since="2026-06-05", until="2026-06-06",
        out_dir=tmp_path / "d", sender=FakeSender(), limit=500, seen=seen, delay_seconds=0,
    )
    assert pushed == 1
    assert len(alerts) == 1                          # partial loss surfaced
    assert SeenStore.key("-100y", 30) in seen        # still marked seen (HWM consistency)


def test_send_media_single_photo_with_caption(httpx_mock: HTTPXMock, tmp_path):
    f = tmp_path / "a.jpg"
    f.write_bytes(b"fakejpg")
    httpx_mock.add_response(
        url="https://api.telegram.org/bot-TOKEN-/sendPhoto",
        method="POST", json={"ok": True, "result": {"message_id": 5}},
    )
    s = TelegramSender(bot_token="-TOKEN-", chat_id="12345")
    assert s.send_media(str(f), "photo", caption="📢 <b>x</b>") == 5
    body = httpx_mock.get_request().read().decode(errors="ignore")
    assert "name=\"caption\"" in body and "parse_mode" in body


def test_send_card_429_is_bounded_not_infinite(httpx_mock: HTTPXMock, mocker):
    # Three consecutive 429s must terminate (raise), never loop forever.
    mocker.patch("chat_daily_tg.tg_sender.time.sleep")
    # retry_max_attempts=3 → rl_hits hits the cap on the 3rd 429 and raises.
    for _ in range(3):
        httpx_mock.add_response(
            url="https://api.telegram.org/bot-TOKEN-/sendMessage",
            method="POST", status_code=429,
            json={"ok": False, "parameters": {"retry_after": 1}},
        )
    s = TelegramSender(bot_token="-TOKEN-", chat_id="12345")
    import pytest
    with pytest.raises(RuntimeError, match="429"):
        s.send_card("hi", link=None)


def test_send_media_mixed_album_tolerates_one_item_failure(httpx_mock: HTTPXMock, tmp_path, mocker):
    from chat_daily_tg.private_media import _send_media
    mocker.patch("chat_daily_tg.tg_sender.time.sleep")
    f1 = tmp_path / "a.jpg"; f1.write_bytes(b"x")
    f2 = tmp_path / "b.pdf"; f2.write_bytes(b"y")
    # photo succeeds; document 400s on every retry (no degrade for media) → that item fails
    httpx_mock.add_response(url="https://api.telegram.org/bot-TOKEN-/sendPhoto",
                            method="POST", json={"ok": True, "result": {"message_id": 1}})
    httpx_mock.add_response(url="https://api.telegram.org/bot-TOKEN-/sendDocument",
                            method="POST", status_code=400, json={"ok": False, "description": "bad"})
    s = TelegramSender(bot_token="-TOKEN-", chat_id="12345", retry_max_attempts=1)
    # mixed types → individual sends; one failing item must NOT raise (≥1 succeeded)
    _send_media([(str(f1), "photo"), (str(f2), "document")], s, caption="cap")


def test_send_media_mixed_album_caption_moves_to_first_success(httpx_mock: HTTPXMock, tmp_path, mocker):
    """If the caption-bearing first item fails but a later item succeeds, the caption
    must ride on that later item — otherwise the post is marked seen and the verbatim
    text is permanently dropped."""
    from chat_daily_tg.private_media import _send_media
    mocker.patch("chat_daily_tg.tg_sender.time.sleep")
    f1 = tmp_path / "a.pdf"; f1.write_bytes(b"x")
    f2 = tmp_path / "b.jpg"; f2.write_bytes(b"y")
    # document (first, would carry the caption) 400s; photo (second) succeeds
    httpx_mock.add_response(url="https://api.telegram.org/bot-TOKEN-/sendDocument",
                            method="POST", status_code=400, json={"ok": False, "description": "bad"})
    httpx_mock.add_response(url="https://api.telegram.org/bot-TOKEN-/sendPhoto",
                            method="POST", json={"ok": True, "result": {"message_id": 2}})
    s = TelegramSender(bot_token="-TOKEN-", chat_id="12345", retry_max_attempts=1)
    _send_media([(str(f1), "document"), (str(f2), "photo")], s, caption="正文 cap")
    photo_req = [r for r in httpx_mock.get_requests() if r.url.path.endswith("/sendPhoto")][0]
    body = photo_req.read().decode("utf-8", "replace")
    assert 'name="caption"' in body and "正文 cap" in body  # caption rode on the successful item


def test_send_media_group_album(httpx_mock: HTTPXMock, tmp_path):
    files = []
    for i in range(3):
        f = tmp_path / f"{i}.jpg"
        f.write_bytes(b"x")
        files.append((str(f), "photo"))
    httpx_mock.add_response(
        url="https://api.telegram.org/bot-TOKEN-/sendMediaGroup",
        method="POST",
        json={"ok": True, "result": [{"message_id": 1}, {"message_id": 2}, {"message_id": 3}]},
    )
    s = TelegramSender(bot_token="-TOKEN-", chat_id="12345")
    ids = s.send_media_group(files, caption="cap")
    assert ids == [1, 2, 3]
    body = httpx_mock.get_request().read().decode(errors="ignore")
    assert "sendMediaGroup" not in body  # method is in URL, not body
    assert "attach://file0" in body and "attach://file2" in body


def test_dump_channel_preflight_rejects_missing_kabi_python(tmp_path, monkeypatch):
    import pytest
    from chat_daily_tg import private_media
    monkeypatch.setattr(private_media, "TG_CLI_PYTHON", str(tmp_path / "no-such-python"))
    with pytest.raises(RuntimeError, match="kabi-tg-cli"):
        private_media.dump_channel("-100x", "2026-01-01", "2026-01-02",
                                   tmp_path / "out", limit=10)


def test_media_post_bypasses_l1_dedup_but_text_only_dup_is_skipped(tmp_path, monkeypatch):
    """A4 false-kill guard: a media post whose caption is a bare URL already in
    the ContentSeenStore still DELIVERS — its photos are unseen content and a
    seen-based skip would terminally lose them (宁可重复不可误杀). The same
    bare-URL caption on a TEXT-ONLY post IS the duplicate and is skipped."""
    from chat_daily_tg import private_media
    from chat_daily_tg.config import RawChannel
    from chat_daily_tg.content_seen import ContentSeenStore, fingerprints_for
    from chat_daily_tg.raw_seen import SeenStore
    import chat_daily_tg.dedup_journal as dj

    url = "https://example.com/2026/dist-systems-consistency"
    manifest = [
        {"msg_id": 60, "date": "2026-06-05T10:00:00+08:00", "text": url,
         "grouped_id": None, "media": [{"path": "/x/60.jpg", "kind": "photo"}]},
        {"msg_id": 61, "date": "2026-06-05T10:05:00+08:00", "text": url,
         "grouped_id": None, "media": []},
    ]
    monkeypatch.setattr(private_media, "dump_channel", lambda *a, **k: manifest)
    journal: list[dict] = []
    monkeypatch.setattr(dj, "record", lambda entry, path=None: journal.append(entry))

    store = ContentSeenStore(tmp_path / "cs.db")
    # The bare link was already delivered elsewhere (e.g. a public text card).
    store.register(fingerprints_for(url), chat_id="-100earlier", msg_id=5,
                   channel="先行频道")

    class FakeSender:
        def __init__(self):
            self.media_calls = []
            self.cards = []

        def send_media(self, path, kind, caption=""):
            self.media_calls.append((path, kind, caption))
            return 1

        def send_card(self, text_html, link=None):
            self.cards.append((text_html, link))
            return [2]

    sender = FakeSender()
    seen = SeenStore(tmp_path / "seen.txt")
    pushed = private_media.push_private_channel(
        channel=RawChannel(id="-100priv", name="私有频道P"),
        since="2026-06-05", until="2026-06-06",
        out_dir=tmp_path / "dump", sender=sender, limit=500,
        seen=seen, delay_seconds=0, content_store=store,
    )
    assert pushed == 1                       # media post delivered, text dup skipped
    assert len(sender.media_calls) == 1      # the photo went out...
    _, kind, caption = sender.media_calls[0]
    assert kind == "photo" and url in caption and "私有频道P" in caption
    assert sender.cards == []                # ...the text-only duplicate did not

    # The text-only skip was journaled as an L1 url hit; the media post was not.
    assert len(journal) == 1
    e = journal[0]
    assert e["layer"] == "L1" and e["action"] == "skip" and e["reason"] == "url"
    assert e["msg_id"] == 61 and e["chat_id"] == "-100priv"

    # Both terminal outcomes advance the seen store (delivery and skip alike).
    assert SeenStore.key("-100priv", 60) in seen
    assert SeenStore.key("-100priv", 61) in seen
    store.close()
