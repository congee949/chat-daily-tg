from __future__ import annotations

import json
from pathlib import Path
import os
import subprocess
import sys

import pytest

from chat_daily_tg.intent_feedback import (
    ContentItem,
    FeedbackStore,
    IdempotencyConflictError,
    KeywordTopicClassifier,
    MessageRef,
    cosine_similarity,
    default_topic_classifier,
    hashed_token_features,
    topic_slug,
)


def _rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_feedback_is_archived_by_topic_and_output_mode(tmp_path):
    store = FeedbackStore(tmp_path / "work")
    result = store.record(
        "expand",
        content=ContentItem(
            "telegram:-100:10",
            topic="AI / 工具",
            text="一段需要进一步展开的内容",
            source=MessageRef(-100123, 10, thread_id=42),
        ),
        target=MessageRef(-100456, 91, thread_id=7),
        idempotency_key="button:91:expand",
        occurred_at="2026-08-24T09:00:00+08:00",
    )

    assert result.created
    assert result.archive_path == tmp_path / "work/topics/ai-工具/expand/events.jsonl"
    assert result.event["schema"] == "intent-feedback.v1"
    assert result.event["intent"] == result.event["output_intensity"] == "expand"
    assert result.event["source"] == {
        "mapping_status": "confirmed",
        "chat_id": -100123,
        "message_id": 10,
        "thread_id": 42,
        "kind": "telegram",
    }
    assert result.event["target"]["message_id"] == 91
    assert _rows(result.event_path) == [dict(result.event)]
    assert _rows(result.archive_path) == [dict(result.event)]


def test_same_idempotency_key_is_append_idempotent(tmp_path):
    store = FeedbackStore(tmp_path)
    kwargs = dict(content_id="item-1", topic="news", text="hello", idempotency_key="r:1")
    first = store.record("read", **kwargs)
    second = store.record("read", **kwargs)

    assert first.created
    assert second.duplicate
    assert len(_rows(tmp_path / "events.jsonl")) == 1
    assert len(_rows(tmp_path / "topics/news/read/events.jsonl")) == 1


def test_separate_cli_processes_share_the_idempotency_lock(tmp_path):
    root = tmp_path / "shared"
    command = [
        sys.executable,
        "-m",
        "chat_daily_tg.intent_feedback",
        "record",
        "read",
        "--root",
        str(root),
        "--content-id",
        "item-cross-process",
        "--topic",
        "news",
        "--text",
        "same event",
        "--idempotency-key",
        "telegram:update:99:read",
        "--occurred-at",
        "2026-08-24T09:00:00+08:00",
    ]
    env = dict(os.environ)
    source_root = str(Path(__file__).resolve().parents[1] / "src")
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (source_root, env.get("PYTHONPATH", "")) if part
    )
    processes = [
        subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
        for _ in range(2)
    ]
    results = [process.communicate(timeout=10) for process in processes]

    assert [process.returncode for process in processes] == [0, 0], results
    assert len(_rows(root / "events.jsonl")) == 1
    assert len(_rows(root / "topics/news/read/events.jsonl")) == 1
    assert (root / ".archive.lock").stat().st_mode & 0o777 == 0o600


def test_reusing_idempotency_key_for_different_intent_fails(tmp_path):
    store = FeedbackStore(tmp_path)
    store.record("read", content_id="item-1", topic="news", text="hello", idempotency_key="r:1")
    with pytest.raises(IdempotencyConflictError):
        store.record("filter", content_id="item-1", topic="news", text="hello", idempotency_key="r:1")


def test_unknown_source_does_not_fabricate_chat_or_message_id(tmp_path):
    result = FeedbackStore(tmp_path).record(
        "filter",
        content_id="url:https://example.test/a",
        topic="noise",
        text="a noisy item",
        source={"chat_id": -100123},  # partial mapping must fail closed
    )
    assert result.event["source"] == {"mapping_status": "unknown"}
    assert "chat_id" not in result.event["source"]
    assert "message_id" not in result.event["source"]


def test_topic_classifier_is_local_and_explicit_topic_wins(tmp_path):
    classifier = KeywordTopicClassifier({"research": ["paper", "论文"], "finance": ["stock"]})
    store = FeedbackStore(tmp_path)
    result = store.record(
        "switch",
        content_id="item-2",
        topic="my-topic",
        text="paper stock",
        topic_classifier=classifier,
    )
    assert result.event["topic"]["label"] == "my-topic"
    assert result.event["topic"]["method"] == "explicit"

    fallback = store.record(
        "switch",
        content_id="item-3",
        text="论文",
        topic_classifier=classifier,
        idempotency_key="item-3:switch",
    )
    assert fallback.event["topic"]["label"] == "research"
    assert fallback.event["topic"]["method"] == "keyword"


def test_default_big_topic_taxonomy_is_local_and_avoids_latin_substrings(tmp_path):
    store = FeedbackStore(tmp_path)
    ai = store.record(
        "expand",
        content_id="ai-item",
        text="本地 embedding 与大模型 Agent 的自动化工作流",
        topic_classifier=default_topic_classifier(),
    )
    assert ai.event["topic"]["label"] == "AI 与自动化"
    assert ai.archive_path.parent.parent.name == "ai-与自动化"

    # The short keyword "ai" must not match the letters inside "training".
    training = store.record(
        "read",
        content_id="training-item",
        text="strength training for runners",
        topic_classifier=default_topic_classifier(),
    )
    assert training.event["topic"]["label"] == "unclassified"


def test_default_big_topic_taxonomy_classifies_ai_glasses_without_lightweight_false_positive(tmp_path):
    store = FeedbackStore(tmp_path)
    result = store.record(
        "switch",
        content_id="youtube-ai-glasses",
        title="没有摄像头 没有扬声器 却做出了一副真正的 AI 眼镜",
        text=(
            "雷鸟 iO 智能眼镜采用光波导，并通过舍弃摄像头和扬声器实现极致轻量化。"
            "其产品价值在于通知显示和全天录音。"
        ),
        topic_classifier=default_topic_classifier(),
    )

    assert result.event["topic"]["label"] == "科技与产品"
    assert result.archive_path.parent.parent.name == "科技与产品"


def test_quantitative_finance_keyword_still_matches_outside_lightweight_compound(tmp_path):
    result = FeedbackStore(tmp_path).record(
        "read",
        content_id="quant-finance",
        text="这是一套量化交易策略",
        topic_classifier=default_topic_classifier(),
    )

    assert result.event["topic"]["label"] == "金融与市场"


def test_offline_features_are_deterministic():
    first = hashed_token_features("AI 工具 AI", dimensions=32)
    second = hashed_token_features("AI 工具 AI", dimensions=32)
    assert first == second
    assert cosine_similarity(first, second) == pytest.approx(1.0)


def test_topic_slug_is_path_safe_and_stable():
    assert topic_slug("AI / 工具") == "ai-工具"
    assert "/" not in topic_slug("../unknown/topic")
    assert topic_slug("") == "unclassified"


def test_cli_records_event_without_remote_calls(tmp_path, capsys):
    from chat_daily_tg.intent_feedback import main

    assert main(
        [
            "record",
            "read",
            "--root",
            str(tmp_path),
            "--content-id",
            "item-4",
            "--topic",
            "reading",
            "--text",
            "hello",
            "--source-chat-id",
            "-1001",
            "--source-message-id",
            "7",
        ]
    ) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["duplicate"] is False
    assert output["source"]["message_id"] == 7
    assert (tmp_path / "topics/reading/read/events.jsonl").exists()


def test_topic_reclassification_keeps_fact_log_and_audits_materialized_view(tmp_path):
    root = tmp_path / "feedback"
    store = FeedbackStore(root)
    recorded = store.record(
        "switch",
        content_id="glasses",
        topic="金融与市场",
        text="AI 眼镜轻量化产品",
        idempotency_key="real-switch",
    )
    original = _rows(root / "events.jsonl")[0]

    corrected = store.reclassify_topic(
        recorded.event_id,
        "科技与产品",
        reason="轻量化不应命中量化金融",
        occurred_at="2026-08-24T17:30:00+08:00",
    )

    assert not corrected.duplicate
    assert _rows(root / "events.jsonl") == [original]
    assert _rows(root / "topics/金融与市场/switch/events.jsonl") == []
    projection = _rows(root / "topics/科技与产品/switch/events.jsonl")
    assert projection[0]["event_id"] == recorded.event_id
    assert projection[0]["topic"]["label"] == "科技与产品"
    audit = _rows(root / "topic_reclassifications.jsonl")
    assert audit[0]["old_topic"]["label"] == "金融与市场"
    assert audit[0]["new_topic"]["label"] == "科技与产品"

    retry = store.reclassify_topic(
        recorded.event_id,
        "科技与产品",
        reason="retry repairs the projection",
    )
    assert retry.duplicate
    assert len(_rows(root / "topic_reclassifications.jsonl")) == 1
    assert len(_rows(root / "topics/科技与产品/switch/events.jsonl")) == 1
