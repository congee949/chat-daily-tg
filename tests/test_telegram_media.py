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
