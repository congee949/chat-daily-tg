import shutil
from pathlib import Path

import pytest

from chat_daily_tg.card_renderer import (
    CardData,
    card_caption,
    parse_concise_to_card,
    render_card_png,
    _find_chrome,
)

SAMPLE = "\n".join([
    "### 🌅 今日总览",
    "今天 3 个群比较活跃，重点是开户和 AI 工具。",
    "",
    "### 💰 钱 / 活动",
    "- **恒生开户**：返现 50 元，值得做（理财群 / 09:12）",
    "- 美团红包活动（羊毛群 / 10:30）",
    "",
    "### 🧠 AI / 工具",
    "- 新出的 4.3 模型据说不错",
    "",
    "### ⚠️ 风险 / 待验证",
    "- 某 USDT 高息盘，疑似资金盘，待验证",
    "",
    "### 🔗 资源",
    "- 一份开户教程",
    "",
    "### 🧾 详情",
    "- /Users/example/chat-daily/archive/2026-04-17/summary.md",
])


def test_parse_buckets_sections():
    card = parse_concise_to_card(SAMPLE, "2026-04-17")
    assert card.date == "2026-04-17"
    assert card.overview and "今天 3 个群" in card.overview[0]
    assert len(card.money) == 2
    assert card.ai_tools == ["新出的 4.3 模型据说不错"]
    assert len(card.risks) == 1
    assert card.resources == ["一份开户教程"]


def test_parse_strips_bullets_and_bold():
    card = parse_concise_to_card(SAMPLE, "2026-04-17")
    # leading "- " removed and "**" stripped, source tail kept
    assert card.money[0] == "恒生开户：返现 50 元，值得做（理财群 / 09:12）"


def test_parse_reduces_markdown_links_to_label():
    md = "### 🔗 资源\n- [看板](https://example.com/a?x=1&y=2)：实时监控"
    card = parse_concise_to_card(md, "2026-04-17")
    assert card.resources == ["看板：实时监控"]
    assert "http" not in card.resources[0]


def test_parse_strips_img_citation_markers():
    # [IMGn] only means something to resolve_citations in the text push; on the
    # PNG card it would render as a literal bracket token.
    md = "### 🧠 AI / 工具\n- **主题**：结论 [IMG1]（电丸）\n- 次要 [IMG2]。"
    card = parse_concise_to_card(md, "2026-04-17")
    assert card.ai_tools == ["主题：结论（电丸）", "次要。"]


def test_parse_drops_detail_section():
    card = parse_concise_to_card(SAMPLE, "2026-04-17")
    # 🧾 详情 (local path) must not leak into any card field
    flat = card.overview + card.money + card.ai_tools + card.risks + card.repeats + card.resources
    assert not any("summary.md" in x for x in flat)


def test_risk_count_and_sections_order():
    card = parse_concise_to_card(SAMPLE, "2026-04-17")
    assert card.risk_count == 1
    keys = [k for k, _ in card.sections()]
    assert keys == ["overview", "money", "ai_tools", "risks", "resources"]


def test_card_caption_has_header_and_truncates():
    card = CardData(date="2026-04-17", overview=["x" * 2000])
    cap = card_caption(card)
    assert cap.startswith("📨 每日群聊速览 · 2026-04-17")
    assert len(cap) <= 1024


def test_empty_card_renders_nothing(tmp_path):
    card = CardData(date="2026-04-17")  # no sections
    assert render_card_png(card, tmp_path / "card.png") is None


@pytest.mark.skipif(_find_chrome() is None, reason="no Chrome/Chromium installed")
def test_render_produces_valid_png(tmp_path):
    card = parse_concise_to_card(SAMPLE, "2026-04-17")
    out = render_card_png(card, tmp_path / "card.png")
    assert out is not None
    data = Path(out).read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"   # PNG magic
    assert len(data) > 1000
