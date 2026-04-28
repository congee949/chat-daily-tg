from pathlib import Path
import json

from chat_daily_tg.media import (
    extract_wx_media_candidates,
    media_paths_from_raw_json,
    score_media_context,
)


def test_score_media_context_prefers_high_value_keywords():
    high, reason = score_media_context("这个活动入口截图，返现额度和风控规则在图里")
    low, _ = score_media_context("哈哈 笑死 好图")

    assert high > 0.6
    assert "活动" in reason
    assert low < high


def test_extract_wx_media_candidates_uses_nearby_context():
    raw = """### 2026-04-27 10:00

**A**: 这个活动入口看图，满减规则在截图里

### 2026-04-27 10:01

**A**: [图片] local_id=2932

### 2026-04-27 10:02

**B**: 这个优惠还能用吗
"""

    items = extract_wx_media_candidates(raw, group_name="群 A")

    assert len(items) == 1
    assert items[0].platform == "微信"
    assert items[0].group_name == "群 A"
    assert items[0].sender_name == "A"
    assert items[0].raw_ref == "local_id=2932"
    assert items[0].score > 0.6


def test_media_paths_from_raw_json_finds_existing_image(tmp_path: Path):
    image = tmp_path / "a.png"
    image.write_bytes(b"x")
    raw = json.dumps({"message": {"photo": {"path": str(image)}}})

    assert media_paths_from_raw_json(raw) == [str(image)]
