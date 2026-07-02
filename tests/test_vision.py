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


def test_resolve_citations_splits_on_valid_markers():
    id_map = {1: _analysis("/x/1.jpg"), 2: _analysis("/x/2.jpg")}
    text = "开头文字\n[IMG1]\n中间文字\n[IMG2]\n结尾文字"

    segments = resolve_citations(text, id_map)

    # each text chunk pairs with the image whose marker ended it — the final
    # tail (after the last marker) has no image.
    assert [img.candidate.local_path if img else None for _, img in segments] == [
        "/x/1.jpg", "/x/2.jpg", None,
    ]
    assert segments[0][0] == "开头文字\n"
    assert segments[1][0] == "\n中间文字\n"
    assert segments[2][0] == "\n结尾文字"


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
