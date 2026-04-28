from pathlib import Path

from chat_daily_tg.repeat_topics import (
    RepeatTopicDB,
    TopicMention,
    mentions_from_json,
    recent_repeat_summary,
    topic_id,
)


def test_topic_id_normalizes_punctuation_and_spaces():
    assert topic_id("Codex Pro 额度重置") == topic_id("Codex-Pro 额度 重置")


def test_upsert_many_tracks_seen_dates_and_mentions(tmp_path: Path):
    db = RepeatTopicDB(tmp_path / "repeat_topics.jsonl")
    db.upsert_many([
        TopicMention(title="Codex Pro 额度重置", summary="首次出现", source_group="示例群")
    ], seen_date="2026-04-27")
    [topic] = db.upsert_many([
        TopicMention(
            title="Codex-Pro 额度 重置",
            summary="继续出现",
            source_group="示例群",
            has_new_information=False,
        )
    ], seen_date="2026-04-28")

    assert topic.mention_count == 2
    assert topic.seen_dates == ["2026-04-27", "2026-04-28"]
    assert topic.consecutive_days == 2
    assert topic.is_repeat() is True


def test_recent_repeat_summary_respects_cooldown(tmp_path: Path):
    path = tmp_path / "repeat_topics.jsonl"
    db = RepeatTopicDB(path)
    db.upsert_many([TopicMention(title="旧活动", summary="old")], seen_date="2026-04-01")
    db.upsert_many([TopicMention(title="近期待验证", summary="recent")], seen_date="2026-04-25")

    summary = recent_repeat_summary(path, today="2026-04-28", cooldown_days=7)

    assert "近期待验证" in summary
    assert "旧活动" not in summary


def test_mentions_from_json_ignores_empty_titles():
    mentions = mentions_from_json([
        {"title": "", "summary": "skip"},
        {
            "title": "X Money",
            "summary": "支付产品",
            "source_group": "示例TG群A",
            "has_new_information": True,
            "new_information": "新增利率传闻",
        },
    ])

    assert len(mentions) == 1
    assert mentions[0].title == "X Money"
    assert mentions[0].has_new_information is True
