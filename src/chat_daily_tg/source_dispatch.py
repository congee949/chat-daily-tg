"""Bounded dispatch for independent daily-summary source lanes.

The source CLIs have different runtime constraints: the WeChat daemon should
not receive concurrent exports, while Telegram text/media work shares one
session and database.  A lane therefore keeps its source-export loop serial;
its explicitly bounded media sub-work may still overlap, while the independent
WeChat and Telegram lanes overlap their I/O.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from time import perf_counter
from typing import Callable, Generic, TypeVar
import logging


log = logging.getLogger(__name__)

T = TypeVar("T")


@dataclass(frozen=True)
class SourceLane(Generic[T]):
    """One isolated source execution lane.

    ``target_mode`` is deliberately explicit and observable.  It identifies
    the CLI/session mode selected for this lane (for example ``wechat_cli`` or
    ``telegram_cli``), rather than implying that a GUI window must be opened.
    """

    name: str
    target_mode: str
    runner: Callable[[], T]


def run_source_lanes(
    lanes: list[SourceLane[T]] | tuple[SourceLane[T], ...],
    *,
    max_workers: int | None = None,
) -> list[T]:
    """Run source lanes concurrently and return results in lane input order.

    Each lane's runner owns its source-export loop and any explicitly bounded
    media sub-work. This dispatcher only overlaps independent lanes, bounds the
    number of threads, rejects ambiguous
    configuration, preserves deterministic merge order, and isolates a failed
    lane from healthy lanes.  A failed lane is omitted from the returned list
    after a warning; all submitted futures are still joined by the executor
    context before the caller continues.
    """

    lane_list = list(lanes)
    if not lane_list:
        return []
    names = [lane.name for lane in lane_list]
    if any(not name.strip() for name in names):
        raise ValueError("source lane names must be non-empty")
    if len(set(names)) != len(names):
        raise ValueError("source lane names must be unique")
    if any(not lane.target_mode.strip() for lane in lane_list):
        raise ValueError("source lane target_mode must be non-empty")
    if max_workers is not None and max_workers < 1:
        raise ValueError("max_workers must be >= 1")

    workers = min(max_workers or len(lane_list), len(lane_list))

    def run_one(lane: SourceLane[T]) -> T:
        started = perf_counter()
        log.info("source lane start: name=%s target_mode=%s", lane.name, lane.target_mode)
        try:
            return lane.runner()
        finally:
            log.info(
                "source lane complete: name=%s target_mode=%s elapsed=%.1fs",
                lane.name,
                lane.target_mode,
                perf_counter() - started,
            )

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="source-lane") as pool:
        futures = [pool.submit(run_one, lane) for lane in lane_list]
        # Reading in submission order retains source ordering even when a later
        # lane completes first.
        results: list[T] = []
        for lane, future in zip(lane_list, futures):
            try:
                results.append(future.result())
            except Exception as e:
                # A broken source must not discard an independent source's
                # successful export.  Keep the lane identity in the warning so
                # operators can attribute the degraded run without a traceback
                # from a worker thread obscuring the healthy result.
                log.warning(
                    "source lane failed: name=%s target_mode=%s error=%s: %s",
                    lane.name,
                    lane.target_mode,
                    type(e).__name__,
                    e,
                )
        return results
