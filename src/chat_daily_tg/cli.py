"""Public command-line interface for chat-daily-tg.

The command tree makes each mutually exclusive pipeline an explicit command,
while :func:`chat_daily_tg.application.legacy_main` preserves existing wrappers.
"""
from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from datetime import date, datetime, timedelta
import fcntl
import json
import logging
import os
import sys
from zoneinfo import ZoneInfo

import httpx

from chat_daily_tg import application as runtime
from chat_daily_tg.features.channels import application as channels
from chat_daily_tg.features.daily import application as daily
from chat_daily_tg.features.growth import application as growth
from chat_daily_tg.features.media_digest import application as media_digest
from chat_daily_tg.config import load_config
from chat_daily_tg.env import load_env_file
from chat_daily_tg.paths import CONFIG_PATH, DATA_DIR, archive_dir_for


Handler = Callable[[argparse.Namespace], int]
log = logging.getLogger(__name__)


def _add_common_delivery_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--no-push", action="store_true", help="Generate artifacts without Telegram delivery")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="chat-daily", description="Run chat-daily-tg pipelines")
    features = parser.add_subparsers(dest="feature", required=True)

    daily_parser = features.add_parser("daily", help="Build and deliver the daily summary")
    daily_commands = daily_parser.add_subparsers(dest="command", required=True)
    daily_run = daily_commands.add_parser("run", help="Run one daily summary")
    daily_run.add_argument("--date", help="YYYY-MM-DD (default: yesterday)")
    daily_run.add_argument("--model", help="Configured model alias")
    _add_common_delivery_options(daily_run)
    daily_run.add_argument("--skip-if-done", action="store_true", help="No-op after completed delivery")
    daily_run.add_argument("--wait-for-wake", action="store_true", help="Use a freshly synced Watch sleep episode when available")
    daily_run.add_argument("--wake-deadline", default="13:00", metavar="HH:MM", help="Legacy launchd compatibility option")
    daily_run.set_defaults(handler=_run_daily)
    health_followup = daily_commands.add_parser(
        "health-followup", help="Deliver synced overnight sleep once without rebuilding the daily summary",
    )
    health_followup.add_argument("--date", help="Daily archive date YYYY-MM-DD (default: yesterday)")
    _add_common_delivery_options(health_followup)
    health_followup.set_defaults(handler=_run_health_followup)

    channels_parser = features.add_parser("channels", help="Forward configured Telegram channels")
    channels_commands = channels_parser.add_subparsers(dest="command", required=True)
    channels_run = channels_commands.add_parser("run", help="Forward unseen channel messages")
    _add_common_delivery_options(channels_run)
    channels_run.set_defaults(handler=_run_channels)
    channels_resend = channels_commands.add_parser("resend", help="Rebuild and resend one channel message")
    channels_resend.add_argument("message", metavar="CHAT_ID:MSG_ID")
    channels_resend.set_defaults(handler=_resend_channel)

    growth_parser = features.add_parser("growth", help="Mine and send growth cards")
    growth_commands = growth_parser.add_subparsers(dest="command", required=True)
    growth_run = growth_commands.add_parser("run", help="Mine today and deliver the next card")
    _add_common_delivery_options(growth_run)
    growth_run.add_argument("--dm-test", action="store_true", help="Deliver to DM without state writes")
    growth_run.add_argument("--model", help="Configured model alias")
    growth_run.set_defaults(handler=_run_growth)
    growth_mine = growth_commands.add_parser("mine", help="Mine one date into the queue without delivery")
    growth_mine.add_argument("--date", required=True, help="YYYY-MM-DD")
    growth_mine.add_argument("--model", help="Configured model alias")
    growth_mine.set_defaults(handler=_mine_growth)
    growth_backfill = growth_commands.add_parser("backfill", help="Backfill configured historical dates")
    growth_backfill.add_argument("--model", help="Configured model alias")
    growth_backfill.set_defaults(handler=_backfill_growth)
    growth_weekly = growth_commands.add_parser("weekly", help="Send weekly growth A/B report")
    _add_common_delivery_options(growth_weekly)
    growth_weekly.add_argument("--model", help="Configured model alias")
    growth_weekly.set_defaults(handler=_weekly_growth)

    bilibili_parser = features.add_parser("bilibili", help="Build the Bilibili subscription digest")
    bilibili_commands = bilibili_parser.add_subparsers(dest="command", required=True)
    bilibili_run = bilibili_commands.add_parser("run", help="Run the Bilibili digest")
    _add_common_delivery_options(bilibili_run)
    bilibili_run.set_defaults(handler=_run_bilibili)

    youtube_parser = features.add_parser("youtube", help="Build the YouTube subscription digest")
    youtube_commands = youtube_parser.add_subparsers(dest="command", required=True)
    youtube_run = youtube_commands.add_parser("run", help="Run the YouTube digest")
    _add_common_delivery_options(youtube_run)
    youtube_run.set_defaults(handler=_run_youtube)
    return parser


def _run_daily(args: argparse.Namespace) -> int:
    return daily.run(date=args.date, model=args.model, no_push=args.no_push,
                     skip_if_done=args.skip_if_done, wait_for_wake=args.wait_for_wake,
                     wake_deadline=args.wake_deadline)


def _run_health_followup(args: argparse.Namespace) -> int:
    """Refresh Health artifacts and deliver only the pending Health section."""
    from chat_daily_tg.health_briefing import (
        build_health_report, format_health_briefing, write_health_gap_record,
    )
    from chat_daily_tg.health_card import render_health_card
    from chat_daily_tg.health_rich import build_health_rich_markdown
    from chat_daily_tg.tg_sender import AmbiguousDeliveryError, TelegramSender

    load_env_file(DATA_DIR / ".env")
    cfg = load_config(CONFIG_PATH)
    if not cfg.health_briefing.enabled:
        return 0
    tz = ZoneInfo(cfg.schedule.timezone)
    report_day = date.fromisoformat(args.date) if args.date else (
        datetime.now(tz).date() - timedelta(days=1)
    )
    archive_dir = archive_dir_for(report_day.isoformat())
    marker = archive_dir / ".health-card-sent"
    ambiguous_marker = archive_dir / ".health-followup-ambiguous"
    if marker.exists():
        return 0
    if ambiguous_marker.exists():
        log.error("health followup delivery needs review: %s", ambiguous_marker)
        return 1
    # A followup belongs to a delivered digest. Never start the daily pipeline
    # or create an archive solely because a scheduler probed before the digest.
    if not any((archive_dir / name).exists() for name in (".run-complete", ".digest-sent")):
        return 0
    with (archive_dir / ".health-followup.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        if marker.exists():
            return 0
        if ambiguous_marker.exists():
            return 1
        report = build_health_report(report_day, cfg.health_briefing, cfg.schedule.timezone)
        if report is None:
            return 0
        write_health_gap_record(report, archive_dir / "health-data-gaps.json")
        if report.last_night_sleep is None:
            return 0
        plain = format_health_briefing(report)
        chart = render_health_card(report, archive_dir / "health-card.png")
        rich = build_health_rich_markdown(
            report, chart_media_id="health_chart" if chart else None,
        )
        (archive_dir / "health-briefing.md").write_text(plain, encoding="utf-8")
        (archive_dir / "health-rich.md").write_text(rich, encoding="utf-8")
        morning = _morning_card_state(archive_dir)
        if morning is not None:
            return _update_morning_card(
                args, cfg, archive_dir, report_day, report, morning, marker, ambiguous_marker, tz,
            )
        if args.no_push:
            return 0
        chat_id, thread_id = runtime.resolve_tg_target(
            "chat_daily", os.environ[cfg.telegram.chat_id_env],
        )
        with TelegramSender(
            bot_token=os.environ[cfg.telegram.bot_token_env],
            chat_id=chat_id, message_thread_id=thread_id,
            retry_max_attempts=cfg.retry.max_attempts,
            retry_backoff_seconds=cfg.retry.backoff_seconds,
        ) as sender:
            try:
                try:
                    sender.send_rich_message(
                        markdown=rich,
                        media=[("health_chart", str(chart), "photo")] if chart else [],
                    )
                except httpx.HTTPStatusError as exc:
                    # Only a deterministic rich-format rejection permits a text
                    # fallback; a lost transport response may already be delivered.
                    if exc.response.status_code != 400:
                        raise
                    sender.send(
                        plain, parse_mode="HTML",
                        state_path=archive_dir / ".health-followup-text-state.json",
                    )
            except AmbiguousDeliveryError as exc:
                ambiguous_marker.write_text(
                    f"{datetime.now(tz).isoformat()} {exc.method}\n", encoding="utf-8",
                )
                log.error("health followup delivery needs review: %s", exc)
                return 1
        marker.write_text(datetime.now(tz).isoformat(), encoding="utf-8")
    return 0


def _morning_card_state(archive_dir) -> dict | None:
    path = archive_dir / runtime.MORNING_CARD_MARKER
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8") or "{}")
    except ValueError:
        return {}


def _update_morning_card(args, cfg, archive_dir, report_day, report, morning,
                         marker, ambiguous_marker, tz) -> int:
    """Redraw the morning card with synced sleep and replace it in place."""
    from chat_daily_tg.morning_card import build_panel, morning_caption, render_morning_card
    from chat_daily_tg.tg_sender import AmbiguousDeliveryError, TelegramSender

    energy_records = None
    energy_cfg = getattr(cfg, "energy_usage", None)
    if energy_cfg is not None and energy_cfg.enabled:
        try:
            from chat_daily_tg.energy_usage import read_energy_records
            energy_records = read_energy_records(energy_cfg)
        except Exception as exc:
            log.warning("energy ledger unavailable for morning card update: %s", exc)
    panel = build_panel(report_day, report, energy_records)
    png = render_morning_card(panel, archive_dir / "morning-card.png")
    if png is None or args.no_push:
        return 0
    caption = morning_caption(panel)
    chat_id, thread_id = runtime.resolve_tg_target(
        "chat_daily", os.environ[cfg.telegram.chat_id_env],
    )
    with TelegramSender(
        bot_token=os.environ[cfg.telegram.bot_token_env],
        chat_id=chat_id, message_thread_id=thread_id,
        retry_max_attempts=cfg.retry.max_attempts,
        retry_backoff_seconds=cfg.retry.backoff_seconds,
    ) as sender:
        message_id = morning.get("message_id")
        try:
            if message_id:
                try:
                    sender.edit_photo(message_id, png, caption=caption, parse_mode="HTML")
                except httpx.HTTPStatusError as exc:
                    # Rejected edit (message deleted or too old): send a fresh card.
                    if exc.response.status_code != 400:
                        raise
                    message_id = sender.send_photo(png, caption=caption, parse_mode="HTML")
            else:
                message_id = sender.send_photo(png, caption=caption, parse_mode="HTML")
        except AmbiguousDeliveryError as exc:
            ambiguous_marker.write_text(
                f"{datetime.now(tz).isoformat()} {exc.method}\n", encoding="utf-8",
            )
            log.error("morning card update needs review: %s", exc)
            return 1
    (archive_dir / runtime.MORNING_CARD_MARKER).write_text(json.dumps({
        "message_id": message_id, "sleep": True, "sent_at": datetime.now(tz).isoformat(),
    }), encoding="utf-8")
    marker.write_text(datetime.now(tz).isoformat(), encoding="utf-8")
    return 0


def _run_channels(args: argparse.Namespace) -> int:
    return channels.run(no_push=args.no_push)


def _resend_channel(args: argparse.Namespace) -> int:
    return channels.resend(args.message)


def _run_growth(args: argparse.Namespace) -> int:
    return growth.run(no_push=args.no_push, dm_test=args.dm_test, model=args.model)


def _mine_growth(args: argparse.Namespace) -> int:
    return growth.mine(date=args.date, model=args.model)


def _backfill_growth(args: argparse.Namespace) -> int:
    return growth.backfill(model=args.model)


def _weekly_growth(args: argparse.Namespace) -> int:
    return growth.weekly(no_push=args.no_push, model=args.model)


def _run_bilibili(args: argparse.Namespace) -> int:
    return media_digest.run_bilibili(no_push=args.no_push)


def _run_youtube(args: argparse.Namespace) -> int:
    return media_digest.run_youtube(no_push=args.no_push)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    normalized_argv = list(sys.argv[1:] if argv is None else argv)
    # A Telegram supergroup id starts with ``-100``. argparse otherwise treats
    # ``chat-daily channels resend -100…:42`` as an option, so accept the
    # natural recovery-hatch spelling without requiring an obscure ``--``.
    if (normalized_argv[:2] == ["channels", "resend"]
            and len(normalized_argv) >= 3 and normalized_argv[2].startswith("-")
            and ":" in normalized_argv[2]
            and normalized_argv[2] != "--"):
        normalized_argv.insert(2, "--")
    args = parser.parse_args(normalized_argv)
    runtime.prepare_process()
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
