"""Application entrypoint for the daily-summary pipeline."""
from __future__ import annotations

from chat_daily_tg import application as _legacy


def run(*, date: str | None, model: str | None, no_push: bool,
        skip_if_done: bool, wait_for_wake: bool, wake_deadline: str) -> int:
    return _legacy.main(
        date_str=date,
        model_alias=model,
        no_push=no_push,
        skip_if_done=skip_if_done,
        wait_for_wake=wait_for_wake,
        wake_deadline=wake_deadline,
    )
