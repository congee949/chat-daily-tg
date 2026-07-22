"""Public command-line interface for chat-daily-tg.

The command tree makes each mutually exclusive pipeline an explicit command,
while :func:`chat_daily_tg.application.legacy_main` preserves existing wrappers.
"""
from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
import sys

from chat_daily_tg import application as runtime
from chat_daily_tg.features.channels import application as channels
from chat_daily_tg.features.daily import application as daily
from chat_daily_tg.features.growth import application as growth
from chat_daily_tg.features.media_digest import application as media_digest


Handler = Callable[[argparse.Namespace], int]


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
