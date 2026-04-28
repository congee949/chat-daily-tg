import base64

from pytest_httpx import HTTPXMock

from chat_daily_tg.media import MediaCandidate
from chat_daily_tg.vision import VisionClient, analyze_media_candidates, vision_markdown


def _candidate(path: str, score: float = 0.8) -> MediaCandidate:
    return MediaCandidate(
        platform="Telegram",
        group_name="群 A",
        timestamp="2026-04-27T02:00:00+00:00",
        sender_name="Alice",
        media_type="图片",
        local_path=path,
        context="活动入口和返现截图",
        reason="高价值",
        score=score,
    )


def test_vision_client_posts_image_and_parses_json(tmp_path, httpx_mock: HTTPXMock):
    image = tmp_path / "a.png"
    image.write_bytes(b"fake image")
    httpx_mock.add_response(
        url="https://vision.example/v1/chat/completions",
        method="POST",
        json={
            "choices": [{
                "message": {
                    "content": '{"type":"activity_poster","value_score":0.8,"summary":"活动","key_facts":["满减"],"risk_flags":["待验证"],"should_include_in_daily":true,"reason":"有活动信息"}'
                }
            }]
        },
    )

    client = VisionClient(endpoint="https://vision.example/v1", model="vision", api_key="k")
    out = client.analyze(_candidate(str(image)))

    assert out.type == "activity_poster"
    assert out.value_score == 0.8
    assert out.should_include_in_daily is True
    req = httpx_mock.get_request()
    body = req.read().decode()
    encoded = base64.b64encode(b"fake image").decode("ascii")
    assert encoded in body


def test_analyze_media_candidates_filters_low_score_and_missing_path(tmp_path, httpx_mock: HTTPXMock):
    image = tmp_path / "a.jpg"
    image.write_bytes(b"fake image")
    httpx_mock.add_response(
        url="https://vision.example/v1/chat/completions",
        method="POST",
        json={
            "choices": [{
                "message": {
                    "content": '{"type":"risk_screenshot","value_score":0.7,"summary":"风险","key_facts":[],"risk_flags":["封号"],"should_include_in_daily":true,"reason":"有风险"}'
                }
            }]
        },
    )
    client = VisionClient(endpoint="https://vision.example/v1", model="vision", api_key="k")

    out = analyze_media_candidates(
        client=client,
        candidates=[
            _candidate(str(image), score=0.8),
            _candidate(str(image), score=0.1),
            _candidate("", score=0.9),
        ],
    )

    assert len(out) == 1
    assert "风险" in vision_markdown(out)
