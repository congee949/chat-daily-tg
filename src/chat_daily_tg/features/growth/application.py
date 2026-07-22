"""Application entrypoints for growth workflows."""
from __future__ import annotations

from chat_daily_tg import application as _legacy


def run(*, no_push: bool, dm_test: bool, model: str | None) -> int:
    return _legacy.run_growth(no_push=no_push, dm_test=dm_test, model_alias=model)


def mine(*, date: str, model: str | None) -> int:
    return _legacy.run_growth(no_push=True, dm_test=False, model_alias=model, mine_date=date)


def backfill(*, model: str | None) -> int:
    return _legacy.run_growth_backfill(model_alias=model)


def weekly(*, no_push: bool, model: str | None) -> int:
    return _legacy.run_growth_weekly(no_push=no_push, model_alias=model)
