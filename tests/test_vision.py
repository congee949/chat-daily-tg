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


def test_analyze_media_candidates_excludes_below_fallback_floor(tmp_path, httpx_mock: HTTPXMock):
    image = tmp_path / "a.jpg"
    _real_image(image)
    httpx_mock.add_response(
        url="https://vision.example/v1/chat/completions",
        method="POST",
        json={
            "choices": [{
                "message": {
                    "content": '{"type":"price_screenshot","value_score":0.6,"summary":"价格","key_facts":["满减"],"risk_flags":[],"should_include_in_daily":true,"reason":"一般"}'
                }
            }]
        },
    )
    client = VisionClient(endpoint="https://vision.example/v1", model="vision", api_key="k")

    out = analyze_media_candidates(client=client, candidates=[_candidate(str(image), score=0.8)])

    # 0.6 misses BOTH the 0.8 include bar and the 0.65 zero-image fallback floor.
    assert out == []


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

    # [IMG9] isn't in id_map so it's stripped with no split point (its padding
    # space eaten too); the merged text up to the next VALID marker [IMG1]
    # pairs with that marker's image.
    assert len(segments) == 2
    assert segments[0][0] == "前面 后面 "
    assert segments[0][1].candidate.local_path == "/x/1.jpg"
    assert segments[1][0] == " 尾巴"
    assert segments[1][1] is None


def test_resolve_citations_dedupes_repeated_id_preferring_section_over_overview():
    # 2026-07-12 regression: [IMG1] cited under BOTH 今日总览 and AI/工具 inlined
    # the same article screenshot twice. The kept occurrence must be the
    # section one; the overview marker is stripped without splitting there.
    id_map = {1: _analysis("/x/1.jpg")}
    text = (
        "### 🌅 今日总览\n- 巨头反目：苹果起诉 OpenAI [IMG1]。\n\n"
        "### 🧠 AI / 工具\n- **苹果起诉 OpenAI**：详情 [IMG1]（电丸）\n\n"
        "### ⚠️ 风险\n- 无"
    )

    segments = resolve_citations(text, id_map)

    images = [img for _, img in segments if img]
    assert len(images) == 1
    assert "今日总览" in segments[0][0] and "AI / 工具" in segments[0][0]
    full_text = "".join(t for t, _ in segments)
    assert "[IMG" not in full_text
    assert "风险" in full_text  # text after the kept marker survives


def test_resolve_citations_duplicate_markers_do_not_consume_image_budget():
    # 2026-07-10 regression class: more markers than unique ids hit the
    # max_images cut and the repeated id crowded a unique image out (real case
    # was 5 markers / 4 ids; fixture uses 4 / 3). Dedup runs BEFORE the cut.
    id_map = {i: _analysis(f"/x/{i}.jpg") for i in range(1, 4)}
    text = "甲 [IMG1] 乙 [IMG1] 丙 [IMG2] 丁 [IMG3] 尾"

    segments = resolve_citations(text, id_map, max_images=3)

    images = [img for _, img in segments if img]
    assert [i.candidate.local_path for i in images] == [
        "/x/1.jpg", "/x/2.jpg", "/x/3.jpg",
    ]
    full_text = "".join(t for t, _ in segments)
    assert "[IMG" not in full_text


def test_resolve_citations_moves_marker_past_closing_punctuation():
    # "…破裂 [IMG1]。" must not orphan the "。" onto the chunk after the image.
    id_map = {1: _analysis("/x/1.jpg"), 2: _analysis("/x/2.jpg")}
    text = "- 结论 [IMG1]。\n\n### 💰 钱 / 活动\n- 主题 [IMG2]（电丸）\n- 其他"

    segments = resolve_citations(text, id_map)

    assert segments[0][0] == "- 结论。"
    assert segments[0][1].candidate.local_path == "/x/1.jpg"
    assert segments[1][0].endswith("- 主题（电丸）")
    assert segments[1][1].candidate.local_path == "/x/2.jpg"
    assert segments[2][0] == "\n- 其他"


def test_resolve_citations_dedup_prefers_any_section_over_overview():
    # Not only AI/工具: a 钱/活动 occurrence must also beat the overview one.
    id_map = {1: _analysis("/x/1.jpg")}
    text = (
        "### 🌅 今日总览\n- 总览提到活动 [IMG1]\n\n"
        "### 💰 钱 / 活动\n- 活动详情 [IMG1]\n\n"
        "### ⚠️ 风险\n- 无"
    )

    segments = resolve_citations(text, id_map)

    images = [img for _, img in segments if img]
    assert len(images) == 1
    # The split happens at the 钱/活动 bullet, so the overview text and the
    # section bullet share the first chunk (kept marker retains its padding).
    assert segments[0][0].rstrip().endswith("- 活动详情")
    assert "总览提到活动" in segments[0][0]
    assert "[IMG" not in "".join(t for t, _ in segments)


def test_resolve_citations_moves_marker_past_halfwidth_tail():
    id_map = {1: _analysis("/x/1.jpg")}
    text = "- point [IMG1] (source).\nnext"

    segments = resolve_citations(text, id_map)

    # The swap eats the marker's own padding (right call for the dominant
    # full-width case), so the tail reattaches without the original space.
    assert segments[0][0] == "- point(source)."
    assert segments[0][1] is not None
    assert segments[1][0] == "\nnext"


def test_strip_citation_markers_removes_all_markers_and_padding():
    from chat_daily_tg.vision import strip_citation_markers

    assert strip_citation_markers("面临破裂 [IMG1]。后续") == "面临破裂。后续"
    assert strip_citation_markers("无标记文本") == "无标记文本"


def test_build_citation_block_dedupes_same_file():
    # The same local file analyzed twice must get ONE id — two ids for one image
    # would sidestep resolve_citations' per-id dedup and inline the photo twice.
    a = _analysis("/x/same.jpg", value_score=0.9)
    b = _analysis("/x/same.jpg", value_score=0.85)
    c = _analysis("/x/other.jpg", value_score=0.8)

    _, id_map = build_citation_block([a, b, c])

    paths = [item.candidate.local_path for item in id_map.values()]
    assert sorted(paths) == ["/x/other.jpg", "/x/same.jpg"]


def test_build_citation_block_same_file_keeps_highest_score():
    # First-seen must not shadow a later, higher-scoring analysis of the same
    # file — the collapse must never demote the image in the ranking.
    low = _analysis("/x/same.jpg", value_score=0.5)
    high = _analysis("/x/same.jpg", value_score=0.99)
    other = _analysis("/x/other.jpg", value_score=0.8)

    _, id_map = build_citation_block([low, high, other])

    same = next(i for i in id_map.values() if i.candidate.local_path == "/x/same.jpg")
    assert same.value_score == 0.99
    assert id_map[1].candidate.local_path == "/x/same.jpg"  # ranked by kept score


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


def test_normalize_score_rescales_and_rejects_off_scale_ratings():
    from chat_daily_tg.vision import _normalize_score
    assert _normalize_score(0.85) == 0.85          # in-range untouched
    assert _normalize_score(8.5) == 0.85           # 0-10 scale → /10
    assert _normalize_score(2.5) == 0.25           # qwen-style drift → /10
    assert _normalize_score(10.0) == 1.0           # top of 0-10 scale → /10
    assert _normalize_score(1.0) == 1.0
    assert _normalize_score(0) == 0.0
    assert _normalize_score(None) == 0.0
    assert _normalize_score("bad") == 0.0           # non-numeric → 0.0
    assert _normalize_score(-3) == 0.0              # negative → untrustworthy → 0.0
    assert _normalize_score(15) == 0.15             # (10, 100] → percent scale → /100
    assert _normalize_score(101) == 0.0             # beyond every rescale window → 0.0


def test_coerce_include_flag_trusts_only_explicit_bool():
    from chat_daily_tg.vision import _coerce_include_flag
    assert _coerce_include_flag(True) is True
    assert _coerce_include_flag(False) is False     # explicit veto honoured
    assert _coerce_include_flag(None) is True        # missing → include (score gate decides)
    assert _coerce_include_flag("false") is True     # non-bool → not a trusted veto


def _vision_response(value_score, *, include="MISSING"):
    payload = {
        "type": "risk_screenshot", "value_score": value_score, "summary": "风险",
        "key_facts": ["封号"], "risk_flags": ["封号"], "reason": "有风险",
    }
    if include != "MISSING":
        payload["should_include_in_daily"] = include
    import json as _json
    return {"choices": [{"message": {"content": _json.dumps(payload, ensure_ascii=False)}}]}


def test_analyze_media_candidates_honours_should_include_veto(tmp_path, httpx_mock: HTTPXMock):
    image = tmp_path / "a.jpg"
    _real_image(image)
    # High enough score, but the model vetoes inclusion → excluded.
    httpx_mock.add_response(
        url="https://vision.example/v1/chat/completions", method="POST",
        json=_vision_response(0.9, include=False),
    )
    client = VisionClient(endpoint="https://vision.example/v1", model="vision", api_key="k")
    out = analyze_media_candidates(client=client, candidates=[_candidate(str(image), score=0.8)])
    assert out == []


def test_analyze_media_candidates_includes_when_flag_missing(tmp_path, httpx_mock: HTTPXMock):
    image = tmp_path / "a.jpg"
    _real_image(image)
    # No should_include_in_daily field → falls back to the score gate alone.
    httpx_mock.add_response(
        url="https://vision.example/v1/chat/completions", method="POST",
        json=_vision_response(0.9),
    )
    client = VisionClient(endpoint="https://vision.example/v1", model="vision", api_key="k")
    out = analyze_media_candidates(client=client, candidates=[_candidate(str(image), score=0.8)])
    assert len(out) == 1


def test_vision_client_retries_429_then_succeeds(tmp_path, httpx_mock: HTTPXMock, monkeypatch):
    monkeypatch.setattr(VisionClient, "RETRYABLE_BACKOFF", [0.0, 0.0])
    image = tmp_path / "a.png"
    image.write_bytes(b"fake image")
    httpx_mock.add_response(
        url="https://vision.example/v1/chat/completions", method="POST",
        status_code=429,
    )
    httpx_mock.add_response(
        url="https://vision.example/v1/chat/completions", method="POST",
        json=_vision_response(0.9),
    )
    client = VisionClient(endpoint="https://vision.example/v1", model="vision", api_key="k")
    out = client.analyze(_candidate(str(image)))
    assert out.value_score == 0.9
    assert len(httpx_mock.get_requests()) == 2


def test_vision_client_does_not_retry_client_errors(tmp_path, httpx_mock: HTTPXMock, monkeypatch):
    import httpx
    import pytest
    monkeypatch.setattr(VisionClient, "RETRYABLE_BACKOFF", [0.0, 0.0])
    image = tmp_path / "a.png"
    image.write_bytes(b"fake image")
    # 400 (bad payload) won't heal on retry — must raise after ONE request.
    httpx_mock.add_response(
        url="https://vision.example/v1/chat/completions", method="POST",
        status_code=400,
    )
    client = VisionClient(endpoint="https://vision.example/v1", model="vision", api_key="k")
    with pytest.raises(httpx.HTTPStatusError):
        client.analyze(_candidate(str(image)))
    assert len(httpx_mock.get_requests()) == 1


def test_vision_client_gives_up_after_exhausting_retries(tmp_path, httpx_mock: HTTPXMock, monkeypatch):
    import httpx
    import pytest
    monkeypatch.setattr(VisionClient, "RETRYABLE_BACKOFF", [0.0, 0.0])
    image = tmp_path / "a.png"
    image.write_bytes(b"fake image")
    for _ in range(3):
        httpx_mock.add_response(
            url="https://vision.example/v1/chat/completions", method="POST",
            status_code=429,
        )
    client = VisionClient(endpoint="https://vision.example/v1", model="vision", api_key="k")
    with pytest.raises(httpx.HTTPStatusError):
        client.analyze(_candidate(str(image)))
    assert len(httpx_mock.get_requests()) == 3


def test_analyze_media_candidates_reports_api_failures_in_stats(tmp_path, httpx_mock: HTTPXMock, monkeypatch, caplog):
    import logging
    monkeypatch.setattr(VisionClient, "RETRYABLE_BACKOFF", [0.0, 0.0])
    image = tmp_path / "a.jpg"
    _real_image(image)
    for _ in range(3):  # one analyze() = up to 3 HTTP attempts with retry
        httpx_mock.add_response(
            url="https://vision.example/v1/chat/completions", method="POST",
            status_code=500,
        )
    client = VisionClient(endpoint="https://vision.example/v1", model="vision", api_key="k")
    stats: dict = {}
    with caplog.at_level(logging.WARNING, logger="chat_daily_tg.vision"):
        out = analyze_media_candidates(
            client=client, candidates=[_candidate(str(image), score=0.8)],
            stats_out=stats)
    assert out == []
    assert stats["attempted"] == 1
    assert stats["api_failed"] == 1
    assert stats["included"] == 0
    # The failure must leave a trace — this exact silence hid the 2026-07-14 gap.
    assert any("vision analyze failed" in r.message for r in caplog.records)
    # Total API failure is a pipeline outage, not a low-value day → ERROR level.
    assert any(r.levelno == logging.ERROR for r in caplog.records)


def test_analyze_media_candidates_stats_distinguish_below_bar_from_failure(tmp_path, httpx_mock: HTTPXMock):
    image = tmp_path / "a.jpg"
    _real_image(image)
    httpx_mock.add_response(
        url="https://vision.example/v1/chat/completions", method="POST",
        json=_vision_response(0.5),
    )
    client = VisionClient(endpoint="https://vision.example/v1", model="vision", api_key="k")
    stats: dict = {}
    out = analyze_media_candidates(
        client=client, candidates=[_candidate(str(image), score=0.8)],
        stats_out=stats)
    assert out == []
    assert stats == {"skipped_prefilter": 0, "skipped_invalid": 0,
                     "attempted": 1, "api_failed": 0, "filtered_empty": 0,
                     "below_bar": 1, "model_veto": 0, "fallback_included": 0,
                     "included": 0}


def test_analyze_media_candidates_fallback_promotes_best_below_bar(tmp_path, httpx_mock: HTTPXMock):
    img_a = tmp_path / "a.jpg"
    _real_image(img_a)
    img_b = tmp_path / "b.jpg"
    _real_image(img_b)
    # Nothing clears 0.8; 0.7 beats 0.66 → exactly the single best is promoted.
    httpx_mock.add_response(
        url="https://vision.example/v1/chat/completions", method="POST",
        json=_vision_response(0.66),
    )
    httpx_mock.add_response(
        url="https://vision.example/v1/chat/completions", method="POST",
        json=_vision_response(0.7),
    )
    client = VisionClient(endpoint="https://vision.example/v1", model="vision", api_key="k")
    stats: dict = {}
    audit: list = []
    out = analyze_media_candidates(
        client=client,
        candidates=[_candidate(str(img_a), score=0.8), _candidate(str(img_b), score=0.8)],
        stats_out=stats, audit_out=audit)
    assert len(out) == 1
    assert out[0].value_score == 0.7
    assert stats["fallback_included"] == 1
    assert stats["included"] == 1
    assert stats["below_bar"] == 1  # the 0.66 stays below-bar
    assert sorted(row["decision"] for row in audit) == ["below-bar", "fallback-included"]


def test_analyze_media_candidates_no_fallback_when_bar_cleared(tmp_path, httpx_mock: HTTPXMock):
    img_a = tmp_path / "a.jpg"
    _real_image(img_a)
    img_b = tmp_path / "b.jpg"
    _real_image(img_b)
    httpx_mock.add_response(
        url="https://vision.example/v1/chat/completions", method="POST",
        json=_vision_response(0.9),
    )
    httpx_mock.add_response(
        url="https://vision.example/v1/chat/completions", method="POST",
        json=_vision_response(0.7),
    )
    client = VisionClient(endpoint="https://vision.example/v1", model="vision", api_key="k")
    stats: dict = {}
    out = analyze_media_candidates(
        client=client,
        candidates=[_candidate(str(img_a), score=0.8), _candidate(str(img_b), score=0.8)],
        stats_out=stats)
    assert len(out) == 1
    assert out[0].value_score == 0.9
    assert stats["fallback_included"] == 0


def test_analyze_media_candidates_fallback_never_overrides_veto_or_low_score(tmp_path, httpx_mock: HTTPXMock):
    img_a = tmp_path / "a.jpg"
    _real_image(img_a)
    img_b = tmp_path / "b.jpg"
    _real_image(img_b)
    # 0.9 but vetoed; 0.5 below the 0.65 fallback floor → still an imageless day.
    httpx_mock.add_response(
        url="https://vision.example/v1/chat/completions", method="POST",
        json=_vision_response(0.9, include=False),
    )
    httpx_mock.add_response(
        url="https://vision.example/v1/chat/completions", method="POST",
        json=_vision_response(0.5),
    )
    client = VisionClient(endpoint="https://vision.example/v1", model="vision", api_key="k")
    stats: dict = {}
    out = analyze_media_candidates(
        client=client,
        candidates=[_candidate(str(img_a), score=0.8), _candidate(str(img_b), score=0.8)],
        stats_out=stats)
    assert out == []
    assert stats["fallback_included"] == 0


def test_analyze_media_candidates_counts_model_veto_separately(tmp_path, httpx_mock: HTTPXMock):
    image = tmp_path / "a.jpg"
    _real_image(image)
    # High score but explicit veto → model_veto, NOT below_bar: the breakdown
    # must answer "score too low or model said no?" without a replay.
    httpx_mock.add_response(
        url="https://vision.example/v1/chat/completions", method="POST",
        json=_vision_response(0.9, include=False),
    )
    client = VisionClient(endpoint="https://vision.example/v1", model="vision", api_key="k")
    stats: dict = {}
    out = analyze_media_candidates(
        client=client, candidates=[_candidate(str(image), score=0.8)],
        stats_out=stats)
    assert out == []
    assert stats["model_veto"] == 1
    assert stats["below_bar"] == 0


def test_analyze_media_candidates_audit_out_records_every_attempt(tmp_path, httpx_mock: HTTPXMock, monkeypatch):
    monkeypatch.setattr(VisionClient, "RETRYABLE_BACKOFF", [0.0, 0.0])
    ok_img = tmp_path / "a.jpg"
    _real_image(ok_img)
    fail_img = tmp_path / "b.jpg"
    _real_image(fail_img)
    httpx_mock.add_response(
        url="https://vision.example/v1/chat/completions", method="POST",
        json=_vision_response(0.5),
    )
    for _ in range(3):
        httpx_mock.add_response(
            url="https://vision.example/v1/chat/completions", method="POST",
            status_code=500,
        )
    client = VisionClient(endpoint="https://vision.example/v1", model="vision", api_key="k")
    audit: list = []
    analyze_media_candidates(
        client=client,
        candidates=[_candidate(str(ok_img), score=0.8), _candidate(str(fail_img), score=0.8)],
        audit_out=audit)
    assert [row["decision"] for row in audit] == ["below-bar", "api_failed"]
    assert audit[0]["value_score"] == 0.5
    assert "error" in audit[1]


def test_normalize_score_rescales_percent_scale():
    from chat_daily_tg.vision import _normalize_score
    assert _normalize_score(85) == 0.85     # percent drift → /100, not zeroed
    assert _normalize_score(8.5) == 0.85    # 0-10 drift → /10
    assert _normalize_score(0.85) == 0.85
    assert _normalize_score(101) == 0.0     # still-garbage stays untrusted
