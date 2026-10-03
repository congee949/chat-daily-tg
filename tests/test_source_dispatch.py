from __future__ import annotations

import logging
import threading
from types import SimpleNamespace

import pytest

from chat_daily_tg.application import _export_telegram_lane, _export_wechat_lane
from chat_daily_tg.source_dispatch import SourceLane, run_source_lanes


def test_lanes_overlap_but_results_keep_input_order() -> None:
    """Independent lane callbacks may overlap, while merge order is stable."""
    first_started = threading.Event()
    second_finished = threading.Event()
    lock = threading.Lock()
    active = 0
    max_active = 0
    completed: list[str] = []

    def runner(value: str):
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        try:
            if value == "wechat":
                first_started.set()
                # Telegram deliberately finishes first.  The first future is
                # still the WeChat lane, so this proves merge order is based on
                # input order rather than completion order.
                assert second_finished.wait(timeout=2)
            else:
                assert first_started.wait(timeout=2)
            return value
        finally:
            with lock:
                active -= 1
                completed.append(value)
            if value == "telegram":
                # Signal after the callback body and its active-count cleanup,
                # so WeChat cannot finish before Telegram's work is complete.
                second_finished.set()

    lanes = [
        SourceLane(name="wechat", target_mode="wechat_cli", runner=lambda: runner("wechat")),
        SourceLane(name="telegram", target_mode="telegram_cli", runner=lambda: runner("telegram")),
    ]

    assert run_source_lanes(lanes) == ["wechat", "telegram"]
    assert completed == ["telegram", "wechat"]
    assert max_active == 2


def test_each_lane_callback_is_one_serial_runner() -> None:
    """The dispatcher submits one callback per lane; a lane owns serial work."""
    events: list[str] = []
    lane_active = {"wechat": 0, "telegram": 0}

    def serial_runner(name: str):
        # Model the two source operations owned by one lane. They must not be
        # nested or duplicated by the dispatcher.
        for step in ("first", "second"):
            assert lane_active[name] == 0
            lane_active[name] += 1
            events.append(f"{name}:{step}:start")
            events.append(f"{name}:{step}:end")
            lane_active[name] -= 1
        return name

    calls = {name: 0 for name in lane_active}

    def callback(name: str):
        def run():
            calls[name] += 1
            return serial_runner(name)

        return run

    lanes = [
        SourceLane("wechat", "wechat_cli", callback("wechat")),
        SourceLane("telegram", "telegram_cli", callback("telegram")),
    ]
    assert run_source_lanes(lanes) == ["wechat", "telegram"]
    assert calls == {"wechat": 1, "telegram": 1}
    for name in ("wechat", "telegram"):
        assert [event for event in events if event.startswith(name)] == [
            f"{name}:first:start", f"{name}:first:end",
            f"{name}:second:start", f"{name}:second:end",
        ]


def test_max_workers_one_serializes_independent_lanes() -> None:
    first_started = threading.Event()
    release_first = threading.Event()
    second_started = threading.Event()
    result: list[str] = []

    def first():
        first_started.set()
        assert release_first.wait(timeout=2)
        return "first"

    def second():
        second_started.set()
        return "second"

    lanes = [
        SourceLane("first", "first_cli", first),
        SourceLane("second", "second_cli", second),
    ]

    dispatch_thread = threading.Thread(
        target=lambda: result.extend(run_source_lanes(lanes, max_workers=1)),
        daemon=True,
    )
    dispatch_thread.start()
    assert first_started.wait(timeout=2)
    assert not second_started.is_set()
    release_first.set()
    dispatch_thread.join(timeout=2)

    assert not dispatch_thread.is_alive()
    assert result == ["first", "second"]
    assert second_started.is_set()


def test_max_workers_bounds_cross_lane_concurrency() -> None:
    lock = threading.Lock()
    barrier = threading.Barrier(2, timeout=2)
    active = 0
    max_active = 0

    def runner(value: str):
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        try:
            if value in {"a", "b"}:
                barrier.wait()
            return value
        finally:
            with lock:
                active -= 1

    lanes = [
        SourceLane(name, f"{name}_cli", lambda value=value: runner(value))
        for name, value in (("a", "a"), ("b", "b"), ("c", "c"))
    ]
    assert run_source_lanes(lanes, max_workers=2) == ["a", "b", "c"]
    assert max_active == 2


def test_failed_lane_isolated_healthy_result_preserved_and_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    healthy_done = threading.Event()

    def failing():
        raise RuntimeError("lane exploded")

    def sibling():
        healthy_done.set()
        return "ok"

    lanes = [
        SourceLane("broken", "broken_cli", failing),
        SourceLane("healthy", "healthy_cli", sibling),
    ]
    with caplog.at_level(logging.INFO, logger="chat_daily_tg.source_dispatch"):
        results = run_source_lanes(lanes)

    # A failed source is omitted, but the independent healthy result remains
    # available in deterministic input order among successful lanes.
    assert results == ["ok"]
    assert healthy_done.is_set()
    messages = [record.getMessage() for record in caplog.records]
    assert any("source lane start: name=broken target_mode=broken_cli" in m for m in messages)
    assert any("source lane complete: name=broken target_mode=broken_cli" in m for m in messages)
    assert any("source lane complete: name=healthy target_mode=healthy_cli" in m for m in messages)
    assert any(
        "source lane failed: name=broken target_mode=broken_cli "
        "error=RuntimeError: lane exploded" in m
        for m in messages
    )


@pytest.mark.parametrize("workers", [0, -1])
def test_max_workers_must_be_positive(workers: int) -> None:
    lane = SourceLane("one", "one_cli", lambda: "ok")
    with pytest.raises(ValueError, match="max_workers must be >= 1"):
        run_source_lanes([lane], max_workers=workers)


def test_lane_identity_validation() -> None:
    with pytest.raises(ValueError, match="names must be unique"):
        run_source_lanes([
            SourceLane("same", "a", lambda: 1),
            SourceLane("same", "b", lambda: 2),
        ])
    with pytest.raises(ValueError, match="target_mode must be non-empty"):
        run_source_lanes([SourceLane("one", " ", lambda: 1)])


def test_telegram_media_failure_keeps_text_and_continues_next_chat(
    tmp_path, monkeypatch, caplog: pytest.LogCaptureFixture,
) -> None:
    chats = [
        SimpleNamespace(
            id="-1001", name="first", limit=20,
            exclude_senders=[], exclude_patterns=[],
        ),
        SimpleNamespace(
            id="-1002", name="second", limit=20,
            exclude_senders=[], exclude_patterns=[],
        ),
    ]
    cfg = SimpleNamespace(
        sources=SimpleNamespace(
            telegram=SimpleNamespace(
                chats=chats, db_path=str(tmp_path / "messages.db"),
                sync_before_export=False,
            ),
        ),
        sanitize=SimpleNamespace(enabled=False),
        models=SimpleNamespace(vision=SimpleNamespace(enabled=True)),
    )
    export_calls: list[str] = []

    def fake_export_chat(**kwargs):
        export_calls.append(kwargs["chat_name"])
        return SimpleNamespace(
            message_count=1, skipped_count=0,
            content=f"{kwargs['chat_name']} text",
            media_candidates=[],
        )

    media_calls: list[str] = []

    def fake_export_chat_media(**kwargs):
        media_calls.append(kwargs["chat_name"])
        if kwargs["chat_name"] == "first":
            raise RuntimeError("media adapter exploded")
        return ["second-photo"]

    # Exercise the narrow compatibility fallback: an old downloader without
    # the batch protocol may still use the historical per-chat adapter.
    from chat_daily_tg.private_media import DumpManyUnsupported

    def fake_export_chat_media_batch(_requests):
        raise DumpManyUnsupported("legacy downloader")

    monkeypatch.setattr("chat_daily_tg.application.export_chat", fake_export_chat)
    monkeypatch.setattr(
        "chat_daily_tg.telegram_media.export_chat_media", fake_export_chat_media,
    )
    monkeypatch.setattr(
        "chat_daily_tg.telegram_media.export_chat_media_batch", fake_export_chat_media_batch,
    )

    with caplog.at_level(logging.INFO, logger="run_daily"):
        groups, media = _export_telegram_lane(
            cfg, date_str="2026-08-27", next_day="2026-08-28", archive_dir=tmp_path,
        )

    assert export_calls == ["first", "second"]
    assert media_calls == ["first", "second"]
    assert groups == [
        ("Telegram / first", "first text"),
        ("Telegram / second", "second text"),
    ]
    assert media == ["second-photo"]
    assert any(
        record.levelno == logging.WARNING
        and "telegram media export failed for first: media adapter exploded" in record.getMessage()
        for record in caplog.records
    )


def test_telegram_lane_batches_media_once_in_config_order(tmp_path, monkeypatch):
    chats = [
        SimpleNamespace(id="-1001", name="first", limit=20,
                        exclude_senders=[], exclude_patterns=[]),
        SimpleNamespace(id="-1002", name="second", limit=30,
                        exclude_senders=[], exclude_patterns=[]),
    ]
    cfg = SimpleNamespace(
        sources=SimpleNamespace(
            telegram=SimpleNamespace(
                chats=chats, db_path=str(tmp_path / "messages.db"),
                sync_before_export=False,
            ),
        ),
        sanitize=SimpleNamespace(enabled=False),
        models=SimpleNamespace(vision=SimpleNamespace(enabled=True)),
    )

    def fake_export_chat(**kwargs):
        return SimpleNamespace(message_count=1, skipped_count=0,
                               content=kwargs["chat_name"], media_candidates=[])

    batch_calls = []

    def fake_batch(requests):
        batch_calls.append(requests)
        return [f"photo-{request['chat_name']}" for request in requests]

    monkeypatch.setattr("chat_daily_tg.application.export_chat", fake_export_chat)
    monkeypatch.setattr("chat_daily_tg.telegram_media.export_chat_media_batch", fake_batch)

    groups, media = _export_telegram_lane(
        cfg, date_str="2026-08-27", next_day="2026-08-28", archive_dir=tmp_path,
    )
    assert groups == [("Telegram / first", "first"), ("Telegram / second", "second")]
    assert media == ["photo-first", "photo-second"]
    assert len(batch_calls) == 1
    assert [request["chat_id"] for request in batch_calls[0]] == ["-1001", "-1002"]
    assert [request["chat_name"] for request in batch_calls[0]] == ["first", "second"]


def test_wechat_lane_passes_vision_prefilter_threshold_to_exporter(tmp_path, monkeypatch):
    cfg = SimpleNamespace(
        sources=SimpleNamespace(wechat=SimpleNamespace(groups=["G1"])),
        models=SimpleNamespace(vision=SimpleNamespace(min_prefilter_score=0.82)),
        sanitize=SimpleNamespace(enabled=False),
    )
    seen_kwargs = []

    def fake_export_group(**kwargs):
        seen_kwargs.append(kwargs)
        return SimpleNamespace(message_count=1, content="hello", media_candidates=[])

    monkeypatch.setattr("chat_daily_tg.application.export_group", fake_export_group)
    groups, media = _export_wechat_lane(cfg, date_str="2026-08-27", archive_dir=tmp_path)

    assert groups == [("微信 / G1", "hello")]
    assert media == []
    assert seen_kwargs[0]["min_download_score"] == 0.82
