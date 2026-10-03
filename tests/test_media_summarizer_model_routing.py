from __future__ import annotations

from datetime import datetime
import json

import httpx
import pytest

from chat_daily_tg.bilibili_digest import build_summarizer as build_bilibili_summarizer
from chat_daily_tg.bilibili_fetcher import BiliVideo
from chat_daily_tg.config import Config, VisionModel
from chat_daily_tg.youtube_digest import build_summarizer as build_youtube_summarizer
from chat_daily_tg.youtube_fetcher import YtVideo


def _config(source: str) -> Config:
    source_config = {
        source: {
            "enabled": True,
            "digest": {"summary_enabled": True},
            "fetch": {
                "whitelist": (
                    [{"uid": 1}]
                    if source == "bilibili"
                    else [{"channel_id": "UCaaaaaaaaaaaaaaaaaaaaaa"}]
                )
            },
        }
    }
    cfg = Config(
        telegram={"bot_token_env": "TG_BOT_TOKEN", "chat_id_env": "TG_CHAT_ID"},
        llm={"endpoint": "http://summary", "model": "gpt-5.6-sol", "api_key_env": "K"},
        sources=source_config,
    )
    cfg.models.vision = VisionModel(
        enabled=True,
        endpoint="http://vision/v1",
        model="gpt-5.6-luna",
        api_key_env="K",
        extra_body={"reasoning_effort": "xhigh"},
    )
    return cfg


@pytest.mark.parametrize("source", ["bilibili", "youtube"])
def test_media_summarizer_uses_configured_luna_xhigh(source, monkeypatch, tmp_path):
    captured = {}
    original_client = httpx.Client

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"finish_reason": "stop", "message": {"content": "一句话摘要"}}
                ]
            },
        )

    monkeypatch.setenv("K", "test-key")
    monkeypatch.setattr(
        f"chat_daily_tg.{source}_digest.httpx.Client",
        lambda **kwargs: original_client(transport=httpx.MockTransport(handler)),
    )
    cover = tmp_path / "cover.jpg"
    cover.write_bytes(b"test-image")
    if source == "bilibili":
        item = BiliVideo(
            bvid="BV1testtest1",
            title="测试视频",
            author="测试 UP",
            uid=1,
            url="https://www.bilibili.com/video/BV1testtest1",
            cover="https://example.com/cover.jpg",
            publish_time=datetime(2026, 8, 25),
            description="测试简介",
        )
        summary = build_bilibili_summarizer(_config(source))(item, cover)
    else:
        item = YtVideo(
            video_id="testvid0001",
            title="Test video",
            author="Test channel",
            channel_id="UCaaaaaaaaaaaaaaaaaaaaaa",
            url="https://www.youtube.com/watch?v=testvid0001",
            cover="https://example.com/cover.jpg",
            publish_time=datetime(2026, 8, 25),
            description="Test description",
        )
        summary = build_youtube_summarizer(_config(source))(item, cover)

    assert summary == "一句话摘要"
    assert captured["model"] == "gpt-5.6-luna"
    assert captured["reasoning_effort"] == "xhigh"
    assert captured["messages"][0]["content"][1]["type"] == "image_url"
