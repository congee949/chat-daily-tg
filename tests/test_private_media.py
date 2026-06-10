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
