"""Run the separate daily Telegram channel digest for yesterday's posts."""
from __future__ import annotations

import argparse
from datetime import date, timedelta
import logging
import os
import sys

from chat_daily_tg.channel_daily import (
    SYSTEM_PROMPT,
    ChannelDigestResult,
    build_channel_prompt,
    build_raw_markdown,
    collect_channel_exports,
    format_for_telegram,
    write_channel_archive,
)
from chat_daily_tg.config import load_config
from chat_daily_tg.env import load_env_file
from chat_daily_tg.llm_client import LLMClient
from chat_daily_tg.logging_setup import configure_logging
from chat_daily_tg.notifier import notify_failure
from chat_daily_tg.paths import CONFIG_PATH, DATA_DIR, log_file_for
from chat_daily_tg.tg_sender import TelegramSender

log = logging.getLogger("run_channel_daily")


def yesterday_iso() -> str:
    return (date.today() - timedelta(days=1)).isoformat()


def main(date_str: str | None = None, *, dry_run: bool = False) -> int:
    if date_str is None:
        date_str = yesterday_iso()
    configure_logging(log_file_for(date_str))
    try:
        return _run(date_str, dry_run=dry_run)
    except Exception as e:
        log.exception("channel daily pipeline failed: %s", e)
        notify_failure("频道日报失败", f"{type(e).__name__}: {e}\n日志: {log_file_for(date_str)}")
        return 1


def _run(date_str: str, *, dry_run: bool = False) -> int:
    next_day = (date.fromisoformat(date_str) + timedelta(days=1)).isoformat()
    load_env_file(DATA_DIR / ".env")
    cfg = load_config(CONFIG_PATH)
    y, m, d = date_str.split("-")
    archive_dir = DATA_DIR / "archive" / y / m / d
    archive_dir.mkdir(parents=True, exist_ok=True)

    exports = collect_channel_exports(
        date_str=date_str,
        next_day=next_day,
        archive_dir=archive_dir,
        db_path=cfg.sources.telegram.db_path,
        sync_before_export=cfg.sources.telegram.sync_before_export,
    )
    if not exports:
        log.warning("no channel content exported for %s", date_str)
        return 0

    prompt = build_channel_prompt(date_str=date_str, exports=exports)
    raw_markdown = build_raw_markdown(date_str=date_str, exports=exports)
    model = cfg.models.summary
    llm = LLMClient(
        endpoint=model.endpoint,
        model=model.model,
        api_key=os.environ[model.api_key_env],
        max_tokens=model.max_tokens,
        timeout=model.timeout,
        retry_max_attempts=cfg.retry.max_attempts,
        retry_backoff_seconds=cfg.retry.backoff_seconds,
        extra_body=model.extra_body,
    )
    summary, usage = llm.chat(prompt=prompt, system=SYSTEM_PROMPT)
    telegram_text = format_for_telegram(date_str, summary)
    result = ChannelDigestResult(
        date_str=date_str,
        raw_markdown=raw_markdown,
        summary_markdown=telegram_text,
        exports=exports,
        usage=usage,
    )
    paths = write_channel_archive(archive_dir, result)
    log.info("channel daily archive written: %s", paths)

    if dry_run:
        print(telegram_text)
        log.info("dry-run enabled, skipped Telegram push")
        return 0

    tg = TelegramSender(
        bot_token=os.environ[cfg.telegram.bot_token_env],
        chat_id=os.environ[cfg.telegram.chat_id_env],
        retry_max_attempts=cfg.retry.max_attempts,
        retry_backoff_seconds=cfg.retry.backoff_seconds,
    )
    tg.send(telegram_text, parse_mode="HTML")
    log.info("channel daily Telegram push complete")
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--date", help="YYYY-MM-DD (default: yesterday)", default=None)
    p.add_argument("--dry-run", action="store_true", help="Print and archive summary without sending Telegram message")
    args = p.parse_args()
    sys.exit(main(date_str=args.date, dry_run=args.dry_run))
