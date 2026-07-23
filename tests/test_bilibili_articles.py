"""Article-specific Bilibili discovery and delivery contracts."""
from __future__ import annotations

from datetime import datetime
import json
import re

import pytest
from pytest_httpx import HTTPXMock

from chat_daily_tg.bilibili_digest import push_digest
from chat_daily_tg.bilibili_fetcher import (
    BiliApiError,
    BiliArticle,
    BiliVideo,
    fetch_new_articles,
    fetch_new_content,
)
from chat_daily_tg.config import BilibiliSource, Config
from chat_daily_tg.raw_seen import SeenStore


NOW = datetime(2026, 7, 23, 12, 0)
NOW_TS = int(NOW.timestamp())


def _src(*, articles: bool = True) -> BilibiliSource:
    return BilibiliSource(
        enabled=True,
        transport="api",
        fetch={
            "whitelist": [
                {"uid": 111, "name": "专栏甲", "articles": articles},
                {"uid": 222, "name": "视频乙", "articles": False},
            ],
            "lookback_hours": 48,
            "article_per_page": 30,
            "article_max_pages": 3,
        },
    )


def _article(article_id: int = 51618753, **kw) -> BiliArticle:
    defaults = {
        "article_id": article_id,
        "title": "标题 <需要转义>",
        "author": "专栏甲",
        "uid": 111,
        "url": f"https://www.bilibili.com/read/cv{article_id}",
        "publish_time": NOW,
        "summary": "列表提供的摘要",
        "cover": "http://cover/article.jpg",
    }
    defaults.update(kw)
    return BiliArticle(**defaults)


def _cfg() -> Config:
    return Config(
        telegram={"bot_token_env": "TG_BOT_TOKEN", "chat_id_env": "TG_CHAT_ID"},
        llm={"endpoint": "http://x", "model": "m", "api_key_env": "K"},
        sources={"bilibili": {"enabled": True, "fetch": {"whitelist": [{"uid": 111}]},
                              "digest": {"card_delay_seconds": 0.0}}},
    )


class _Sender:
    chat_id = -1001111111111
    message_thread_id = 486

    def __init__(self, *, photo_fails: bool = False) -> None:
        self.photo_fails = photo_fails
        self.cards: list[tuple] = []
        self.photos: list[tuple] = []

    def send_photo(self, path, caption="", parse_mode=None, button=None):
        if self.photo_fails:
            raise RuntimeError("photo failed")
        self.photos.append((path, caption, parse_mode, button))
        return 101

    def send_card(self, text_html, *, link=None, button=None):
        self.cards.append((text_html, link, button))
        return [102]


def test_bilibili_up_articles_defaults_false() -> None:
    assert BilibiliSource(fetch={"whitelist": [{"uid": 1}]}).fetch.whitelist[0].articles is False


def test_article_fetch_is_opt_in_and_filters_dirty_seen_and_expired(
    httpx_mock: HTTPXMock, tmp_path
) -> None:
    httpx_mock.add_response(
        url=re.compile(r"https://api\.bilibili\.com/x/space/article\?.*mid=111.*"),
        json={"code": 0, "data": {"has_more": False, "articles": [
            {"id": 51618753, "title": "新文章", "publish_time": NOW_TS - 60,
             "summary": "摘要", "image_url": "http://cover/new.jpg"},
            {"id": "bad", "title": "坏 id", "publish_time": NOW_TS - 60},
            {"id": 51618754, "title": "太旧", "publish_time": NOW_TS - 49 * 3600},
            {"id": 51618755, "title": "已发送", "publish_time": NOW_TS - 60},
        ]}},
    )
    seen = SeenStore(tmp_path / "seen.txt")
    seen.add("bilibili:article:51618755")
    articles = fetch_new_articles(_src(), seen, now=NOW)
    assert [article.article_id for article in articles] == [51618753]
    assert articles[0].url == "https://www.bilibili.com/read/cv51618753"
    assert articles[0].cover == "http://cover/new.jpg"
    requests = httpx_mock.get_requests()
    assert len(requests) == 1 and "mid=111" in str(requests[0].url)


def test_article_fetch_all_rate_limited_surfaces_alertable_failure(
    httpx_mock: HTTPXMock, tmp_path
) -> None:
    httpx_mock.add_response(
        url=re.compile(r"https://api\.bilibili\.com/x/space/article\?.*mid=111.*"),
        json={"code": -509, "message": "请求过于频繁"},
    )
    with pytest.raises(BiliApiError, match="article lists failed"):
        fetch_new_articles(_src(), SeenStore(tmp_path / "seen.txt"), now=NOW)


def test_article_disabled_makes_no_article_request(httpx_mock: HTTPXMock, tmp_path) -> None:
    assert fetch_new_articles(_src(articles=False), SeenStore(tmp_path / "seen.txt"), now=NOW) == []
    assert httpx_mock.get_requests() == []


def test_content_pipeline_keeps_articles_when_video_transport_is_down(monkeypatch, tmp_path) -> None:
    import chat_daily_tg.bilibili_fetcher as fetcher

    monkeypatch.setattr(fetcher, "fetch_new_videos",
                        lambda *a, **kw: (_ for _ in ()).throw(BiliApiError("video down")))
    monkeypatch.setattr(fetcher, "fetch_new_articles", lambda *a, **kw: [_article()])
    contents = fetch_new_content(_src(), SeenStore(tmp_path / "seen.txt"), now=NOW)
    assert len(contents) == 1 and contents[0].kind == "article"


def test_article_card_fallback_writes_canonical_ledger_and_marks_seen(monkeypatch, tmp_path) -> None:
    from chat_daily_tg import sent_ledger

    ledger = tmp_path / "media_sent_ledger.jsonl"
    monkeypatch.setattr(sent_ledger, "MEDIA_SENT_LEDGER", ledger)
    monkeypatch.setattr("chat_daily_tg.bilibili_digest.download_cover", lambda _url, dest: dest)
    sender = _Sender(photo_fails=True)
    seen = SeenStore(tmp_path / "seen.txt")
    sent = push_digest([_article()], sender=sender, seen=seen, cfg=_cfg(),
                       summarizer=lambda *_: (_ for _ in ()).throw(AssertionError("article summarized")),
                       workdir=tmp_path)
    assert sent == 1 and "bilibili:article:51618753" in seen
    text, link, button = sender.cards[0]
    assert "📄 专栏" in text and "❤️ 标记后" in text
    assert link == "https://www.bilibili.com/read/cv51618753"
    assert button == ("📖 阅读全文", link)
    row = json.loads(ledger.read_text(encoding="utf-8"))
    assert row["url"] == link and row["id"] == "bilibili:article:51618753"
    assert row["message_id"] == 102


def test_no_push_article_does_not_write_seen_or_ledger(tmp_path) -> None:
    seen = SeenStore(tmp_path / "seen.txt")
    assert push_digest([_article()], sender=None, seen=seen, cfg=_cfg(), summarizer=None,
                       workdir=tmp_path, no_push=True) == 0
    assert _article().seen_key not in seen
