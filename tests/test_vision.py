import base64

from pytest_httpx import HTTPXMock

from chat_daily_tg.media import MediaCandidate
from chat_daily_tg.vision import (
    VisionAnalysis, VisionClient, analyze_media_candidates,
    build_citation_block, resolve_citations, vision_markdown,
)


def _candidate(path: str, score: float = 0.8, timestamp: str = "2026-04-27T02:00:00+00:00") -> MediaCandidate:
    return MediaCandidate(
        platform="Telegram",
        group_name="群 A",
        timestamp=timestamp,
        sender_name="Alice",
        media_type="图片",
        local_path=path,
        context="活动入口和返现截图",
        reason="高价值",
        score=score,
    )


def _analysis(path: str = "/x/a.jpg", value_score: float = 0.8, summary: str = "活动截图") -> VisionAnalysis:
    return VisionAnalysis(
        candidate=_candidate(path),
        type="activity_poster",
        value_score=value_score,
        summary=summary,
        key_facts=["满减"],
        risk_flags=[],
        should_include_in_daily=True,
        reason="有活动信息",
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


def _real_image(path, size=(400, 400)):
    """A real JPEG big enough to clear the thumbnail gate (>=10KB, >=300x300)."""
    import random
    from PIL import Image as PILImage
    img = PILImage.new("RGB", size)
    img.putdata([(random.randint(0, 255), random.randint(0, 255), random.randint(0, 255))
                 for _ in range(size[0] * size[1])])
    img.save(path, quality=95)
    assert path.stat().st_size >= 10 * 1024


def test_analyze_media_candidates_filters_low_score_missing_path_and_thumbnails(tmp_path, httpx_mock: HTTPXMock):
    image = tmp_path / "a.jpg"
    _real_image(image)
    thumb = tmp_path / "thumb.jpg"
    thumb.write_bytes(b"tiny thumbnail bytes")  # < 10KB → thumbnail gate drops it
    httpx_mock.add_response(
        url="https://vision.example/v1/chat/completions",
        method="POST",
        json={
            "choices": [{
                "message": {
                    "content": '{"type":"risk_screenshot","value_score":0.85,"summary":"风险","key_facts":[],"risk_flags":["封号"],"should_include_in_daily":true,"reason":"有风险"}'
                }
            }]
        },
    )
    client = VisionClient(endpoint="https://vision.example/v1", model="vision", api_key="k")

    out = analyze_media_candidates(
        client=client,
        candidates=[
            _candidate(str(image), score=0.8),
            _candidate(str(image), score=0.1),   # below prefilter
            _candidate("", score=0.9),           # no local path
            _candidate(str(thumb), score=0.9),   # thumbnail-sized file
        ],
    )

    assert len(out) == 1
    assert "风险" in vision_markdown(out)


def test_analyze_media_candidates_excludes_below_08_value_score(tmp_path, httpx_mock: HTTPXMock):
    image = tmp_path / "a.jpg"
    _real_image(image)
    httpx_mock.add_response(
        url="https://vision.example/v1/chat/completions",
        method="POST",
        json={
            "choices": [{
                "message": {
                    "content": '{"type":"price_screenshot","value_score":0.7,"summary":"价格","key_facts":["满减"],"risk_flags":[],"should_include_in_daily":true,"reason":"一般"}'
                }
            }]
        },
    )
    client = VisionClient(endpoint="https://vision.example/v1", model="vision", api_key="k")

    out = analyze_media_candidates(client=client, candidates=[_candidate(str(image), score=0.8)])

    assert out == []  # 0.7 < the 0.8 include bar


def test_build_citation_block_ranks_and_caps_by_value_score():
    analyses = [_analysis(f"/x/{i}.jpg", value_score=score) for i, score in enumerate([0.5, 0.9, 0.7, 0.95, 0.6, 0.8])]

    md, id_map = build_citation_block(analyses)

    assert len(id_map) == 5  # capped at MAX_CITATIONS
    ordered_scores = [item.value_score for item in id_map.values()]
    assert ordered_scores == sorted(ordered_scores, reverse=True)
    assert "[IMG1]" in md and "[IMG5]" in md
    assert "[IMG6]" not in md


def test_build_citation_block_empty_input():
    md, id_map = build_citation_block([])
    assert md == ""
    assert id_map == {}


def test_build_citation_block_prefers_telegram_over_wechat():
    from dataclasses import replace as dc_replace
    tg = _analysis("/x/tg.jpg", value_score=0.85)
    wx = _analysis("/x/wx.jpg", value_score=0.95)
    wx = VisionAnalysis(
        candidate=dc_replace(wx.candidate, platform="微信"),
        type=wx.type, value_score=wx.value_score, summary=wx.summary,
        key_facts=wx.key_facts, risk_flags=wx.risk_flags,
        should_include_in_daily=wx.should_include_in_daily, reason=wx.reason,
    )

    _, id_map = build_citation_block([wx, tg])

    # TG image ranks first despite the WeChat one having a higher value_score
    assert id_map[1].candidate.platform == "Telegram"
    assert id_map[2].candidate.platform == "微信"


def test_resolve_citations_splits_on_valid_markers():
    id_map = {1: _analysis("/x/1.jpg"), 2: _analysis("/x/2.jpg")}
    text = "开头文字\n[IMG1]\n中间文字\n[IMG2]\n结尾文字"

    segments = resolve_citations(text, id_map, max_images=2)

    # each text chunk pairs with the image whose marker ended it — the final
    # tail (after the last marker) has no image.
    assert [img.candidate.local_path if img else None for _, img in segments] == [
        "/x/1.jpg", "/x/2.jpg", None,
    ]
    assert segments[0][0] == "开头文字\n"
    assert segments[1][0] == "\n中间文字\n"
    assert segments[2][0] == "\n结尾文字"


def test_resolve_citations_caps_to_three_images_by_default():
    id_map = {i: _analysis(f"/x/{i}.jpg") for i in range(1, 5)}
    text = "甲 [IMG1] 乙 [IMG2] 丙 [IMG3] 丁 [IMG4] 尾"

    segments = resolve_citations(text, id_map)

    images = [img for _, img in segments if img]
    assert len(images) == 3
    # doc order when no AI section: first three kept, fourth stripped
    assert [i.candidate.local_path for i in images] == ["/x/1.jpg", "/x/2.jpg", "/x/3.jpg"]
    full_text = "".join(t for t, _ in segments)
    assert "[IMG" not in full_text  # dropped marker stripped, not leaked


def test_resolve_citations_prefers_ai_section_marker_when_capping():
    id_map = {1: _analysis("/x/money.jpg"), 2: _analysis("/x/ai.jpg")}
    text = (
        "### 💰 钱 / 活动\n- 活动内容 [IMG1]\n\n"
        "### 🧠 AI / 工具\n- AI 内容 [IMG2]\n\n"
        "### ⚠️ 风险\n- 风险内容"
    )

    segments = resolve_citations(text, id_map, max_images=1)

    images = [img for _, img in segments if img]
    assert len(images) == 1
    assert images[0].candidate.local_path == "/x/ai.jpg"
    full_text = "".join(t for t, _ in segments)
    assert "[IMG" not in full_text
    assert "活动内容" in full_text and "风险内容" in full_text


def test_resolve_citations_strips_unknown_id_without_breaking_segment():
    id_map = {1: _analysis("/x/1.jpg")}
    text = "前面 [IMG9] 后面 [IMG1] 尾巴"

    segments = resolve_citations(text, id_map)

    # [IMG9] isn't in id_map so it's stripped with no split point; the merged
    # text up to the next VALID marker [IMG1] pairs with that marker's image.
    assert len(segments) == 2
    assert segments[0][0] == "前面  后面 "
    assert segments[0][1].candidate.local_path == "/x/1.jpg"
    assert segments[1][0] == " 尾巴"
    assert segments[1][1] is None


def test_resolve_citations_no_markers_returns_single_segment():
    segments = resolve_citations("纯文本，没有引用", {1: _analysis()})
    assert segments == [("纯文本，没有引用", None)]


def test_resolve_citations_drops_marker_when_local_path_missing():
    analysis = _analysis("")
    id_map = {1: analysis}
    segments = resolve_citations("前面 [IMG1] 后面", id_map)
    assert len(segments) == 1
    assert segments[0][1] is None
    assert "[IMG1]" not in segments[0][0]
