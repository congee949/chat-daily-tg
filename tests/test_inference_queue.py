from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from chat_daily_tg.inference_queue import (
    CrossProcessInferenceQueue,
    InferenceQueueFull,
)


def test_shared_queue_prioritizes_online_across_independent_clients(tmp_path: Path) -> None:
    path = tmp_path / "queue.sqlite"
    first_client = CrossProcessInferenceQueue("http://runtime/v1", capacity=4, path=path)
    offline_client = CrossProcessInferenceQueue("http://runtime/v1", capacity=4, path=path)
    online_client = CrossProcessInferenceQueue("http://runtime/v1", capacity=4, path=path)
    entered = threading.Event()
    release = threading.Event()
    order: list[str] = []

    def first() -> None:
        with first_client.acquire(online=False, deadline=time.monotonic() + 2):
            order.append("offline-running")
            entered.set()
            assert release.wait(1)

    def queued_offline() -> None:
        with offline_client.acquire(online=False, deadline=time.monotonic() + 2):
            order.append("offline-waiting")

    def queued_online() -> None:
        with online_client.acquire(online=True, deadline=time.monotonic() + 2):
            order.append("online")

    threads = [
        threading.Thread(target=first),
        threading.Thread(target=queued_offline),
        threading.Thread(target=queued_online),
    ]
    threads[0].start()
    assert entered.wait(1)
    threads[1].start()
    time.sleep(0.03)
    threads[2].start()
    time.sleep(0.03)
    release.set()
    for thread in threads:
        thread.join(2)
        assert not thread.is_alive()

    assert order == ["offline-running", "online", "offline-waiting"]


def test_online_admission_fails_fast_when_shared_capacity_is_full(tmp_path: Path) -> None:
    path = tmp_path / "queue.sqlite"
    first_client = CrossProcessInferenceQueue("http://runtime/v1", capacity=1, path=path)
    second_client = CrossProcessInferenceQueue("http://runtime/v1", capacity=1, path=path)
    entered = threading.Event()
    release = threading.Event()

    def running() -> None:
        with first_client.acquire(online=False, deadline=time.monotonic() + 2):
            entered.set()
            assert release.wait(1)

    thread = threading.Thread(target=running)
    thread.start()
    assert entered.wait(1)
    started = time.monotonic()
    with pytest.raises(InferenceQueueFull):
        with second_client.acquire(online=True, deadline=time.monotonic() + 1):
            raise AssertionError("unreachable")
    assert time.monotonic() - started < 0.2
    release.set()
    thread.join(2)
    assert not thread.is_alive()

