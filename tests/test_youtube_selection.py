from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os

import pytest
from pydantic import ValidationError

from chat_daily_tg.config import Config, YoutubeChannel
from chat_daily_tg.raw_seen import SeenStore
from chat_daily_tg.youtube_fetcher import YtVideo
from chat_daily_tg.youtube_selection import (
    Decision, confirmed_x_videos, parse_decisions, select_videos,
)

CH = "UCXZCJLdBC09xxGZ6gcdrc6A"
OTHER = "UCaaaaaaaaaaaaaaaaaaaaaa"


def video(**kw):
    values = dict(video_id="Fls_onRviPM", title="Complete developer keynote",
                  author="OpenAI", channel_id=CH,
                  url="https://www.youtube.com/watch?v=Fls_onRviPM",
                  description="Complete developer keynote with API demos and technical Q&A.",
                  duration_seconds=1200)
    values.update(kw)
    return YtVideo(**values)


def cfg():
    return Config(
        telegram={"bot_token_env": "TOKEN", "chat_id_env": "CHAT"},
        llm={"endpoint": "http://unused", "model": "test", "api_key_env": "KEY"},
        sources={"youtube": {"enabled": True, "fetch": {"whitelist": [
            {"channel_id": CH, "selection": "ai_official"},
            {"channel_id": OTHER},
        ]}}},
    )


def response(v, **changes):
    row = dict(video_id=v.video_id, category="keynote", relevant=True, substantive=True,
               confidence=0.99, evidence=["Complete developer keynote"], takeaway="完整API演示与技术问答")
    row.update(changes)
    return json.dumps({"decisions": [row]}, ensure_ascii=False)


def receipt(path, *, content=None, **changes):
    content = content or "Watch https://youtu.be/Fls_onRviPM?t=30"
    row = dict(schema="sent-content.v1", producer="x_monitor", delivery_state="confirmed",
               chat_id=-100123, message_id=42, url="https://x.com/OpenAI/status/123",
               content=content, content_hash=hashlib.sha256(content.encode()).hexdigest(),
               sent_at=datetime.now(timezone.utc).isoformat())
    row.update(changes)
    path.write_text(json.dumps(row) + "\n")
    return row


def select(tmp_path, videos, **kw):
    return select_videos(
        videos, cfg=cfg(), seen=kw.pop("seen", SeenStore(tmp_path / "seen")),
        ledger_path=tmp_path / "x.jsonl", journal_path=tmp_path / "journal.jsonl", **kw)


def test_policy_is_explicit_and_default_preserves_existing_channels():
    assert YoutubeChannel(channel_id=CH).selection == "all"
    with pytest.raises(ValidationError):
        YoutubeChannel(channel_id=CH, selection="typo")


def test_substantive_video_gets_note_and_remains_unseen_until_delivery(tmp_path):
    v = video()
    seen = SeenStore(tmp_path / "seen")
    kept = select(tmp_path, [v], seen=seen,
                  evaluator=lambda batch: parse_decisions(response(v), batch))
    assert kept[0].selection_note == "视频看点：完整API演示与技术问答"
    assert v.seen_key not in seen
    assert not (tmp_path / "journal.jsonl").exists()


@pytest.mark.parametrize("changes", [
    {"relevant": "false"}, {"substantive": 1}, {"confidence": True},
    {"confidence": float("nan")}, {"confidence": 1.1}, {"category": "news"},
    {"evidence": ["invented video transcript"]}, {"evidence": []},
    {"video_id": "unrequested"}, {"takeaway": "x" * 121},
])
def test_invalid_llm_output_cannot_control_suppression(changes):
    v = video()
    with pytest.raises(ValueError):
        parse_decisions(response(v, **changes), [v])


def test_unknown_and_low_confidence_never_suppress():
    v = video()
    for changes in (dict(category="unknown", relevant=False, substantive=False),
                    dict(category="promo", substantive=False, confidence=0.8),
                    dict(category="promo", substantive=True)):
        assert parse_decisions(response(v, **changes), [v])[v.video_id].action == "keep"


def test_duplicate_or_missing_llm_identity_fails_validation():
    v = video()
    row = json.loads(response(v))["decisions"][0]
    for rows in ([], [row, row]):
        with pytest.raises(ValueError):
            parse_decisions(json.dumps({"decisions": rows}), [v])


def test_same_topic_without_same_video_link_is_retained(tmp_path):
    v = video()
    receipt(tmp_path / "x.jsonl", content="OpenAI developer keynote launches new API features.")
    kept = select(tmp_path, [v], evaluator=lambda batch: parse_decisions(response(v), batch))
    assert len(kept) == 1


def test_exact_x_video_url_skips_with_confirmed_receipt_and_journal_before_seen(tmp_path):
    v = video()
    receipt(tmp_path / "x.jsonl")

    class CheckedSeen(SeenStore):
        def add(self, key):
            assert (tmp_path / "journal.jsonl").exists()
            return super().add(key)

    seen = CheckedSeen(tmp_path / "seen")
    assert select(tmp_path, [v], seen=seen) == []
    assert v.seen_key in SeenStore(tmp_path / "seen")
    row = json.loads((tmp_path / "journal.jsonl").read_text())
    assert row["reason"] == "same_video_already_delivered_on_x"
    assert row["x_receipt"]["message_id"] == 42


@pytest.mark.parametrize("changes", [
    dict(delivery_state="pending"), dict(content_hash="bad"), dict(message_id=0),
    dict(producer="other"), dict(sent_at="2026-01-01T00:00:00"),
    dict(sent_at=(datetime.now(timezone.utc) - timedelta(days=8)).isoformat()),
])
def test_invalid_or_old_x_receipt_cannot_suppress(tmp_path, changes):
    p = tmp_path / "x.jsonl"
    receipt(p, **changes)
    assert confirmed_x_videos(p) == {}


def test_stale_x_ledger_is_ignored(tmp_path):
    p = tmp_path / "x.jsonl"
    receipt(p)
    past = datetime.now().timestamp() - 90000
    os.utime(p, (past, past))
    assert confirmed_x_videos(p) == {}


def test_damaged_rows_do_not_hide_valid_confirmed_receipts(tmp_path):
    p = tmp_path / "x.jsonl"
    receipt(p)
    valid = p.read_text()
    p.write_text("broken record\n" + valid + '{"not complete"\n')
    assert "Fls_onRviPM" in confirmed_x_videos(p)


@pytest.mark.parametrize("url", [
    "https://www.youtube.com/watch?v=Fls_onRviPM&feature=share",
    "https://m.youtube.com/watch?v=Fls_onRviPM",
    "https://www.youtube.com/live/Fls_onRviPM",
    "https://youtu.be/Fls_onRviPM.",
])
def test_canonical_youtube_url_variants_match(tmp_path, url):
    p = tmp_path / "x.jsonl"
    receipt(p, content=url)
    assert "Fls_onRviPM" in confirmed_x_videos(p)


def test_no_push_changes_neither_seen_nor_journal(tmp_path):
    v = video(title="Introducing our new model", description="Available today.")
    seen = SeenStore(tmp_path / "seen")
    assert select(tmp_path, [v], no_push=True, seen=seen) == []
    assert v.seen_key not in seen
    assert not (tmp_path / "seen").exists()
    assert not (tmp_path / "journal.jsonl").exists()


def test_selection_never_filters_other_channels(tmp_path):
    v = video(title="Introducing our new model", channel_id=OTHER)
    assert select(tmp_path, [v]) == [v]


def test_selection_failure_keeps_useful_video_but_filters_obvious_promos(tmp_path):
    useful = video(title="Meet the all new Codex Cloud",
                   description="See how to create a reusable cloud environment.")
    promo = replace(useful, video_id="newpromo001", description="Available today.")

    def failed(_):
        raise RuntimeError("LLM unavailable")

    assert select(tmp_path, [useful, promo], evaluator=failed) == [useful]


def test_business_story_requires_operational_detail(tmp_path):
    promo = video(title="Building the partner ecosystem with Sophos",
                  description="Technology partnerships keep organizations safe.")
    tutorial = replace(promo, video_id="tutorial001",
                       description="Tutorial: configure the API integration step-by-step.")
    kept = select(tmp_path, [promo, tutorial], evaluator=lambda _: {})
    assert kept == [tutorial]
    assert promo.seen_key in SeenStore(tmp_path / "seen")


def test_journal_failure_keeps_video_without_seen_write(tmp_path, monkeypatch):
    from chat_daily_tg import dedup_journal
    monkeypatch.setattr(dedup_journal, "record", lambda *args, **kwargs: False)
    v = video(title="Introducing a new model", description="Available today.")
    seen = SeenStore(tmp_path / "seen")
    assert select(tmp_path, [v], seen=seen) == [v]
    assert v.seen_key not in seen


def test_validated_low_value_result_is_journaled_and_skipped(tmp_path):
    v = video(title="Customer stories", description="Customer stories endorse the new model.")
    result = parse_decisions(response(v, category="promo", substantive=False,
                                      evidence=["Customer stories"], takeaway="客户背书"), [v])
    assert select(tmp_path, [v], evaluator=lambda _: result) == []
    assert v.seen_key in SeenStore(tmp_path / "seen")


def test_low_confidence_fallback_keeps_video_without_note(tmp_path):
    v = video()
    result = parse_decisions(response(v, confidence=0.2), [v])
    kept = select(tmp_path, [v], evaluator=lambda _: result)
    assert len(kept) == 1 and kept[0].selection_note == ""
