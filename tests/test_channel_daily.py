from __future__ import annotations

from pathlib import Path

import pytest

from chat_daily_tg.channel_daily import (
    DEFAULT_CHANNELS,
    ChannelConfig,
    ChannelExport,
    ChannelDigestResult,
    SourceItem,
    build_channel_prompt,
    format_for_telegram,
    validate_summary,
    write_channel_archive,
)


def source(ref: str, channel: str, text: str, msg_id: int = 101) -> SourceItem:
    return SourceItem(
        ref=ref,
        channel=channel,
        msg_id=msg_id,
        timestamp="2026-05-08T01:30:00+00:00",
        text=text,
        url=f"https://t.me/c/1/{msg_id}",
    )


def test_default_channels_match_requested_sources():
    assert [c.name for c in DEFAULT_CHANNELS] == [
        "投机之路",
        "科技圈🎗在花频道📮",
        "LydiaPod",
        "Gary Playa",
    ]
    assert [c.id for c in DEFAULT_CHANNELS] == [
        "-1002631914757",
        "-1001125107539",
        "-1003471026810",
        "-1003452282759",
    ]


def test_build_channel_prompt_contains_date_sections_source_refs_and_content():
    exports = [
        ChannelExport(
            config=ChannelConfig(id="1", name="投机之路", section="市场 / 投机"),
            content="[C01-001] BTC 波动加大",
            message_count=1,
            skipped_count=0,
            sources=[source("C01-001", "投机之路", "BTC 波动加大")],
        ),
        ChannelExport(
            config=ChannelConfig(id="2", name="LydiaPod", section="身心灵 / 健康"),
            content="[C02-001] 睡眠和压力管理",
            message_count=1,
            skipped_count=0,
            sources=[source("C02-001", "LydiaPod", "睡眠和压力管理", msg_id=202)],
        ),
    ]

    prompt = build_channel_prompt(date_str="2026-05-08", exports=exports)

    assert "昨日频道推送｜2026-05-08" in prompt
    assert "市场 / 投机" in prompt
    assert "身心灵 / 健康" in prompt
    assert "[C01-001]" in prompt
    assert "[C02-001]" in prompt
    assert "BTC 波动加大" in prompt
    assert "睡眠和压力管理" in prompt
    assert "每条结论末尾必须带来源编号" in prompt
    assert "不要编造原文没有的信息" in prompt


def test_build_channel_prompt_rejects_empty_exports():
    with pytest.raises(ValueError, match="no channel content"):
        build_channel_prompt(date_str="2026-05-08", exports=[])


def test_validate_summary_rejects_too_short_output():
    with pytest.raises(ValueError, match="too short"):
        validate_summary("太短")


def test_format_for_telegram_adds_channel_digest_title_when_missing():
    text = "## 市场 / 投机\n- BTC 波动加大，关注风险偏好变化和仓位管理。[C01-001]"

    assert format_for_telegram("2026-05-08", text).startswith("📡 昨日频道推送｜2026-05-08")


def test_format_for_telegram_does_not_duplicate_title():
    text = "📡 昨日频道推送｜2026-05-08\n\n## 市场 / 投机\n- BTC 波动加大，关注风险偏好变化和仓位管理。[C01-001]"

    assert format_for_telegram("2026-05-08", text).count("昨日频道推送｜2026-05-08") == 1


def test_content_refs_use_one_ref_per_telegram_message():
    export = ChannelExport(
        config=ChannelConfig(id="1", name="投机之路", section="市场 / 投机"),
        content="# Telegram: 投机之路\n\n[C01-001] [Telegram / 投机之路 / 09:30 / unknown] BTC 波动加大\n\n正文第一段\n\n正文第二段",
        message_count=1,
        skipped_count=0,
        sources=[source("C01-001", "投机之路", "BTC 波动加大")],
    )

    assert export.content.count("[C01-001]") == 1


def test_write_channel_archive_writes_raw_summary_and_source_metadata(tmp_path: Path):
    exports = [
        ChannelExport(
            config=ChannelConfig(id="1", name="投机之路", section="市场 / 投机"),
            content="[C01-001] BTC 波动加大",
            message_count=1,
            skipped_count=0,
            sources=[source("C01-001", "投机之路", "BTC 波动加大")],
        )
    ]
    result = ChannelDigestResult(
        date_str="2026-05-08",
        raw_markdown="# raw",
        summary_markdown="📡 昨日频道推送｜2026-05-08\n\n## 市场 / 投机\n- BTC 波动加大 [C01-001]",
        exports=exports,
        usage={"total_tokens": 123},
    )

    paths = write_channel_archive(tmp_path, result)

    assert paths["raw"].read_text(encoding="utf-8") == "# raw\n"
    assert "BTC 波动加大" in paths["summary"].read_text(encoding="utf-8")
    metadata = paths["metadata"].read_text(encoding="utf-8")
    assert '"total_tokens": 123' in metadata
    assert '"ref": "C01-001"' in metadata
    assert '"url": "https://t.me/c/1/101"' in metadata
