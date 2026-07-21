from datetime import datetime
import json
from pathlib import Path

from chat_daily_tg.config import Config
from chat_daily_tg.raw_seen import SeenStore
from chat_daily_tg.youtube_digest import card_caption, push_digest
from chat_daily_tg.youtube_fetcher import YtVideo

CH = "UCaaaaaaaaaaaaaaaaaaaaaa"


def _video(video_id="testvid0001", **kw):
    defaults = dict(
        video_id=video_id, title="标题 <b>带标签</b>", author="频道甲", channel_id=CH,
        url=f"https://www.youtube.com/watch?v={video_id}",
        cover=f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
        duration="12m40s", duration_seconds=760,
        publish_time=datetime(2026, 7, 2, 8, 0), description="简介", view=45615,
    )
    defaults.update(kw)
    return YtVideo(**defaults)


def _cfg(**digest_overrides) -> Config:
    digest = {"topic": "youtube", "summary_enabled": True, "card_delay_seconds": 0.0}
    digest.update(digest_overrides)
    return Config(
        telegram={"bot_token_env": "TG_BOT_TOKEN", "chat_id_env": "TG_CHAT_ID"},
        llm={"endpoint": "http://x", "model": "m", "api_key_env": "K"},
        sources={"youtube": {"enabled": True, "digest": digest,
                             "fetch": {"whitelist": [{"channel_id": CH}]}}},
    )


class FakeSender:
    def __init__(self, photo_fails=False, chat_id=-1004424841223, message_thread_id=486):
        self.photo_fails = photo_fails
        self.chat_id = chat_id
        self.message_thread_id = message_thread_id
        self.photos: list[tuple] = []
        self.cards: list[tuple] = []
        self._next_id = 100

    def send_photo(self, path, caption="", parse_mode=None, button=None):
        if self.photo_fails:
            raise RuntimeError("photo boom")
        self.photos.append((Path(path), caption, parse_mode, button))
        mid = self._next_id
        self._next_id += 1
        return mid

    def send_card(self, text_html, *, link=None, button=None):
        self.cards.append((text_html, link, button))
        mid = self._next_id
        self._next_id += 1
        return [mid]


# --- card_caption ------------------------------------------------------------

def test_card_caption_escapes_html_and_includes_fields():
    cap = card_caption(_video(), "一句话摘要")
    assert "<b>标题 &lt;b&gt;带标签&lt;/b&gt;</b>" in cap
    assert "频道甲" in cap and "12m40s" in cap
    assert "📝 一句话摘要" in cap
    # URL lives on the watch button + ledger only — not in caption text
    assert "https://www.youtube.com/watch?v=" not in cap
    assert "<a href" not in cap and "🔗" not in cap


def test_card_caption_omits_summary_and_duration_when_absent():
    cap = card_caption(_video(duration=None, duration_seconds=None,
                              publish_time=None, view=None), None)
    assert "📝" not in cap and "<a href" not in cap
    assert "https://www.youtube.com/watch?v=" not in cap


# --- push_digest -------------------------------------------------------------

def test_push_digest_sends_oldest_first_and_marks_seen(monkeypatch, tmp_path):
    monkeypatch.setattr("chat_daily_tg.youtube_digest.download_cover",
                        lambda url, dest: dest)
    sender = FakeSender()
    seen = SeenStore(tmp_path / "seen.txt")
    videos = [_video("newestvid01", publish_time=datetime(2026, 7, 2, 10)),
              _video("oldestvid01", publish_time=datetime(2026, 7, 1, 10))]
    n = push_digest(videos, sender=sender, seen=seen, cfg=_cfg(),
                    summarizer=lambda v, p: "摘要", workdir=tmp_path)
    assert n == 2
    # 直链按钮：无 B 站那种跳转页
    sent_buttons = [b for _, _, _, b in sender.photos]
    assert sent_buttons == [
        ("▶️ 在 YouTube 观看", "https://www.youtube.com/watch?v=oldestvid01"),
        ("▶️ 在 YouTube 观看", "https://www.youtube.com/watch?v=newestvid01"),
    ]
    assert "youtube:newestvid01" in seen and "youtube:oldestvid01" in seen
    # caption does not print URL (watch button + ledger only)
    assert "https://www.youtube.com/watch?v=" not in sender.photos[0][1]


def test_push_digest_photo_failure_falls_back_to_text_card(monkeypatch, tmp_path):
    monkeypatch.setattr("chat_daily_tg.youtube_digest.download_cover",
                        lambda url, dest: dest)
    sender = FakeSender(photo_fails=True)
    seen = SeenStore(tmp_path / "seen.txt")
    n = push_digest([_video()], sender=sender, seen=seen, cfg=_cfg(),
                    summarizer=None, workdir=tmp_path)
    assert n == 1 and len(sender.cards) == 1
    text, link, button = sender.cards[0]
    assert link == "https://www.youtube.com/watch?v=testvid0001"
    assert button == ("▶️ 在 YouTube 观看", "https://www.youtube.com/watch?v=testvid0001")
    assert "https://www.youtube.com/watch?v=testvid0001" not in text
    assert "youtube:testvid0001" in seen


def test_push_digest_link_disabled_omits_button(monkeypatch, tmp_path):
    monkeypatch.setattr("chat_daily_tg.youtube_digest.download_cover",
                        lambda url, dest: dest)
    sender = FakeSender()
    n = push_digest([_video()], sender=sender, seen=SeenStore(tmp_path / "s.txt"),
                    cfg=_cfg(link_enabled=False), summarizer=None, workdir=tmp_path)
    assert n == 1 and sender.photos[0][3] is None


def test_push_digest_cover_download_failure_uses_text_card(monkeypatch, tmp_path):
    monkeypatch.setattr("chat_daily_tg.youtube_digest.download_cover",
                        lambda url, dest: None)
    sender = FakeSender()
    n = push_digest([_video()], sender=sender, seen=SeenStore(tmp_path / "s.txt"),
                    cfg=_cfg(), summarizer=None, workdir=tmp_path)
    assert n == 1 and sender.photos == [] and len(sender.cards) == 1


def test_push_digest_total_failure_leaves_unseen_for_retry(monkeypatch, tmp_path):
    monkeypatch.setattr("chat_daily_tg.youtube_digest.download_cover",
                        lambda url, dest: None)

    class DeadSender(FakeSender):
        def send_card(self, text_html, *, link=None, button=None):
            raise RuntimeError("card boom")

    seen = SeenStore(tmp_path / "seen.txt")
    n = push_digest([_video()], sender=DeadSender(), seen=seen, cfg=_cfg(),
                    summarizer=None, workdir=tmp_path)
    assert n == 0
    assert "youtube:testvid0001" not in seen  # next run retries it


def test_push_digest_no_push_does_not_mark_seen(tmp_path):
    seen = SeenStore(tmp_path / "seen.txt")
    n = push_digest([_video()], sender=None, seen=seen, cfg=_cfg(),
                    summarizer=None, workdir=tmp_path, no_push=True)
    assert n == 0 and "youtube:testvid0001" not in seen


def test_push_digest_writes_sent_ledger(monkeypatch, tmp_path):
    from chat_daily_tg import sent_ledger as sl
    sl.clear_cache()
    ledger = tmp_path / "media_sent_ledger.jsonl"
    monkeypatch.setattr(sl, "MEDIA_SENT_LEDGER", ledger)
    monkeypatch.setattr("chat_daily_tg.sent_ledger.MEDIA_SENT_LEDGER", ledger)
    monkeypatch.setattr("chat_daily_tg.youtube_digest.download_cover",
                        lambda url, dest: dest)
    sender = FakeSender()
    n = push_digest([_video()], sender=sender, seen=SeenStore(tmp_path / "s.txt"),
                    cfg=_cfg(), summarizer=None, workdir=tmp_path)
    assert n == 1
    from chat_daily_tg.sent_ledger import lookup
    hit = lookup(sender.chat_id, 100, path=ledger)
    if not hit:
        rows = ledger.read_text(encoding="utf-8").strip().splitlines()
        assert len(rows) == 1
        row = json.loads(rows[0])
        assert row["url"] == "https://www.youtube.com/watch?v=testvid0001"
        assert row["producer"] == "youtube"
        assert row["message_id"] == 100
        assert row["thread_id"] == 486
        assert row["id"] == "youtube:testvid0001"
    else:
        assert hit["url"] == "https://www.youtube.com/watch?v=testvid0001"
        assert hit["producer"] == "youtube"
        assert hit["id"] == "youtube:testvid0001"
