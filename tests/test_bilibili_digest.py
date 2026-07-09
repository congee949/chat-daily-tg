from datetime import datetime
from pathlib import Path

from chat_daily_tg.bilibili_digest import card_caption, push_digest
from chat_daily_tg.bilibili_fetcher import BiliVideo
from chat_daily_tg.config import Config
from chat_daily_tg.raw_seen import SeenStore


def _video(bvid="BV1testtest1", **kw):
    defaults = dict(
        bvid=bvid, title="标题 <b>带标签</b>", author="UP甲", uid=111,
        url=f"https://www.bilibili.com/video/{bvid}",
        cover="http://cover/x.jpg", duration="8m4s",
        publish_time=datetime(2026, 7, 2, 8, 0), description="简介", view=45615,
    )
    defaults.update(kw)
    return BiliVideo(**defaults)


def _cfg(**digest_overrides) -> Config:
    digest = {"topic": "bilibili", "summary_enabled": True, "card_delay_seconds": 0.0}
    digest.update(digest_overrides)
    return Config(
        telegram={"bot_token_env": "TG_BOT_TOKEN", "chat_id_env": "TG_CHAT_ID"},
        llm={"endpoint": "http://x", "model": "m", "api_key_env": "K"},
        sources={"bilibili": {"enabled": True, "digest": digest,
                              "fetch": {"whitelist": [{"uid": 111}]}}},
    )


class FakeSender:
    def __init__(self, photo_fails=False):
        self.photo_fails = photo_fails
        self.photos: list[tuple] = []
        self.cards: list[tuple] = []

    def send_photo(self, path, caption="", parse_mode=None, button=None):
        if self.photo_fails:
            raise RuntimeError("photo boom")
        self.photos.append((Path(path), caption, parse_mode, button))
        return 1

    def send_card(self, text_html, *, link=None, button=None):
        self.cards.append((text_html, link, button))
        return [1]


# --- card_caption ------------------------------------------------------------

def test_card_caption_escapes_html_and_includes_fields():
    cap = card_caption(_video(), "一句话摘要")
    assert "<b>标题 &lt;b&gt;带标签&lt;/b&gt;</b>" in cap
    # meta line is now just UP主 · 时长 — publish time and view count were dropped
    assert "UP甲" in cap and "8m4s" in cap and "07-02 08:00" not in cap and "45,615播放" not in cap
    assert "📝 一句话摘要" in cap
    # watch link ships as an inline button, never in the caption
    assert "<a href" not in cap and "🔗" not in cap


def test_card_caption_omits_summary_when_absent():
    cap = card_caption(_video(duration=None, publish_time=None, view=None), None)
    assert "📝" not in cap and "<a href" not in cap


# --- push_digest -------------------------------------------------------------

def test_push_digest_sends_oldest_first_and_marks_seen(monkeypatch, tmp_path):
    monkeypatch.setattr("chat_daily_tg.bilibili_digest.download_cover",
                        lambda url, dest: dest)
    sender = FakeSender()
    seen = SeenStore(tmp_path / "seen.txt")
    videos = [_video("BV1newest001", publish_time=datetime(2026, 7, 2, 10)),
              _video("BV1oldest001", publish_time=datetime(2026, 7, 1, 10))]
    n = push_digest(videos, sender=sender, seen=seen, cfg=_cfg(),
                    summarizer=lambda v, p: "摘要", workdir=tmp_path)
    assert n == 2
    # order + watch button both live in the button URL now (caption carries no link)
    sent_buttons = [b for _, _, _, b in sender.photos]
    assert sent_buttons == [
        ("▶️ 在 B 站观看", "https://kanban.congeelife.top:8443/b/BV1oldest001"),
        ("▶️ 在 B 站观看", "https://kanban.congeelife.top:8443/b/BV1newest001"),
    ]
    assert "bilibili:BV1newest001" in seen and "bilibili:BV1oldest001" in seen


def test_push_digest_photo_failure_falls_back_to_text_card(monkeypatch, tmp_path):
    monkeypatch.setattr("chat_daily_tg.bilibili_digest.download_cover",
                        lambda url, dest: dest)
    sender = FakeSender(photo_fails=True)
    seen = SeenStore(tmp_path / "seen.txt")
    n = push_digest([_video()], sender=sender, seen=seen, cfg=_cfg(),
                    summarizer=None, workdir=tmp_path)
    assert n == 1 and len(sender.cards) == 1
    text, link, button = sender.cards[0]
    assert link == "https://www.bilibili.com/video/BV1testtest1"
    assert button == ("▶️ 在 B 站观看", "https://kanban.congeelife.top:8443/b/BV1testtest1")
    assert "bilibili:BV1testtest1" in seen


def test_push_digest_link_disabled_omits_button(monkeypatch, tmp_path):
    monkeypatch.setattr("chat_daily_tg.bilibili_digest.download_cover",
                        lambda url, dest: dest)
    sender = FakeSender()
    n = push_digest([_video()], sender=sender, seen=SeenStore(tmp_path / "s.txt"),
                    cfg=_cfg(link_enabled=False), summarizer=None, workdir=tmp_path)
    assert n == 1 and sender.photos[0][3] is None


def test_push_digest_cover_download_failure_uses_text_card(monkeypatch, tmp_path):
    monkeypatch.setattr("chat_daily_tg.bilibili_digest.download_cover",
                        lambda url, dest: None)
    sender = FakeSender()
    n = push_digest([_video()], sender=sender, seen=SeenStore(tmp_path / "s.txt"),
                    cfg=_cfg(), summarizer=None, workdir=tmp_path)
    assert n == 1 and sender.photos == [] and len(sender.cards) == 1


def test_push_digest_total_failure_leaves_unseen_for_retry(monkeypatch, tmp_path):
    monkeypatch.setattr("chat_daily_tg.bilibili_digest.download_cover",
                        lambda url, dest: None)

    class DeadSender(FakeSender):
        def send_card(self, text_html, *, link=None):
            raise RuntimeError("card boom")

    seen = SeenStore(tmp_path / "seen.txt")
    n = push_digest([_video()], sender=DeadSender(), seen=seen, cfg=_cfg(),
                    summarizer=None, workdir=tmp_path)
    assert n == 0
    assert "bilibili:BV1testtest1" not in seen  # next run retries it


def test_push_digest_no_push_does_not_mark_seen(tmp_path):
    seen = SeenStore(tmp_path / "seen.txt")
    n = push_digest([_video()], sender=None, seen=seen, cfg=_cfg(),
                    summarizer=None, workdir=tmp_path, no_push=True)
    assert n == 0 and "bilibili:BV1testtest1" not in seen
