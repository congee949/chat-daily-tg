from unittest.mock import patch

from chat_daily_tg import telegram_media


def _entry(msg_id, text, media):
    return {
        "msg_id": msg_id,
        "date": "2026-06-10T21:52:00+08:00",
        "text": text,
        "html": text,
        "grouped_id": None,
        "media": media,
    }


def test_keeps_only_photos(tmp_path):
    manifest = [
        _entry(1, "看这个活动", [{"path": "/x/1.jpg", "kind": "photo"}]),
        _entry(2, "片段", [{"path": "/x/2.mp4", "kind": "video"}]),
        _entry(3, "资料", [{"path": "/x/3.pdf", "kind": "document"}]),
        _entry(4, "无媒体", []),
    ]
    with patch.object(telegram_media, "dump_channel", return_value=manifest):
        cands = telegram_media.export_chat_media(
            chat_id="-100", chat_name="g", since="2026-06-10", until="2026-06-11",
            out_dir=tmp_path, limit=50,
        )
    assert len(cands) == 1
    c = cands[0]
    assert c.local_path == "/x/1.jpg"
    assert c.platform == "Telegram"
    assert c.media_type == "图片"
    assert c.raw_ref == "msg_id=1"


def test_score_floor_lets_caption_less_photo_pass_prefilter(tmp_path):
    # A photo whose caption hits no value keyword scores ~0.28 by media.py, which is
    # below the 0.45 vision prefilter; the download floor lifts it to 0.5 so vision sees it.
    manifest = [_entry(1, "随便聊聊", [{"path": "/x/1.jpg", "kind": "photo"}])]
    with patch.object(telegram_media, "dump_channel", return_value=manifest):
        cands = telegram_media.export_chat_media(
            chat_id="-100", chat_name="g", since="s", until="u", out_dir=tmp_path, limit=50,
        )
    assert cands[0].score >= 0.5


def test_caps_to_max_photos_keeping_most_recent(tmp_path):
    manifest = [_entry(i, "t", [{"path": f"/x/{i}.jpg", "kind": "photo"}]) for i in range(10)]
    with patch.object(telegram_media, "dump_channel", return_value=manifest):
        cands = telegram_media.export_chat_media(
            chat_id="-100", chat_name="g", since="s", until="u", out_dir=tmp_path,
            limit=50, max_photos=3,
        )
    assert len(cands) == 3
    # manifest is oldest→newest, so the cap keeps the newest three (ids 7,8,9)
    assert [c.raw_ref for c in cands] == ["msg_id=7", "msg_id=8", "msg_id=9"]


def test_failure_returns_empty_so_text_export_survives(tmp_path):
    with patch.object(telegram_media, "dump_channel", side_effect=RuntimeError("telethon down")):
        cands = telegram_media.export_chat_media(
            chat_id="-100", chat_name="g", since="s", until="u", out_dir=tmp_path, limit=50,
        )
    assert cands == []


def test_zero_photo_budget_skips_telethon_process(tmp_path):
    # An explicit zero budget is a no-op, not a request to fetch and then
    # discard media.  This protects the daily text path from needless startup
    # and authentication work when vision has no photo capacity.
    with patch.object(telegram_media, "dump_channel") as dump:
        got = telegram_media.export_chat_media(
            chat_id="-100", chat_name="g", since="s", until="u", out_dir=tmp_path,
            limit=50, max_photos=0,
        )
    assert got == []
    dump.assert_not_called()


def test_media_candidates_from_manifest_is_pure_and_caps_newest():
    manifest = [
        _entry(1, "old", [{"path": "/x/1.jpg", "kind": "photo"}]),
        _entry(2, "middle", [{"path": "/x/2.jpg", "kind": "photo"}]),
        _entry(3, "new", [{"path": "/x/3.jpg", "kind": "photo"}]),
    ]
    got = telegram_media.media_candidates_from_manifest(
        manifest, chat_name="g", max_photos=2,
    )
    assert [candidate.raw_ref for candidate in got] == ["msg_id=2", "msg_id=3"]
    # The input remains untouched; conversion has no I/O side effects.
    assert manifest[0]["media"][0]["path"] == "/x/1.jpg"


def test_export_chat_media_batch_uses_one_dump_and_isolates_failed_chat(tmp_path):
    results = [
        {"chat_id": "-1001", "status": "ok", "manifest": [
            _entry(1, "one", [{"path": "/x/1.jpg", "kind": "photo"}]),
        ], "error": None},
        {"chat_id": "-1002", "status": "failed", "manifest": [], "error": "down"},
    ]
    seen = []

    def fake_dump(requests):
        seen.append(requests)
        return results

    with patch.object(telegram_media, "dump_channels", side_effect=fake_dump):
        got = telegram_media.export_chat_media_batch([
            {"chat_id": "-1001", "chat_name": "one", "since": "s", "until": "u",
             "out_dir": tmp_path, "limit": 10},
            {"chat_id": "-1002", "chat_name": "two", "since": "s", "until": "u",
             "out_dir": tmp_path, "limit": 10},
        ])
    assert len(seen) == 1
    assert [request["chat_id"] for request in seen[0]] == ["-1001", "-1002"]
    assert all(request["media_kinds"] == ["photo"] for request in seen[0])
    assert all(request["max_media"] == 20 for request in seen[0])
    assert [candidate.raw_ref for candidate in got] == ["msg_id=1"]


def test_batch_zero_photo_budget_skips_subprocess(tmp_path):
    with patch.object(telegram_media, "dump_channels") as dump:
        got = telegram_media.export_chat_media_batch(
            [{"chat_id": "-1001", "chat_name": "one", "since": "s", "until": "u",
              "out_dir": tmp_path, "limit": 10}],
            max_photos=0,
        )
    assert got == []
    dump.assert_not_called()
