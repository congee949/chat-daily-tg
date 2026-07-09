#!/usr/bin/env python
from __future__ import annotations

import argparse
import os
from datetime import date
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from chat_daily_tg.config import load_config
from chat_daily_tg.env import load_env_file, scrub_socks_proxy_env
from chat_daily_tg.llm_client import LLMClient
from chat_daily_tg.paths import CONFIG_PATH, DATA_DIR
from chat_daily_tg.research_loop import ResearchSpec, append_result, run_research_once
from chat_daily_tg.tg_sender import TelegramSender


def main() -> int:
    scrub_socks_proxy_env()
    args = parse_args()
    cfg = None
    llm = None
    tg = None

    if args.sample_output is None or args.send_telegram:
        load_env_file(DATA_DIR / ".env")

    if args.sample_output is None:
        cfg = load_config(args.config)
        api_key = os.environ[cfg.llm.api_key_env]
        llm = LLMClient(
            endpoint=cfg.llm.endpoint,
            model=args.model or cfg.llm.model,
            api_key=api_key,
            max_tokens=args.max_tokens or cfg.llm.max_tokens,
            timeout=cfg.llm.timeout,
            retry_max_attempts=cfg.retry.max_attempts,
            retry_backoff_seconds=cfg.retry.backoff_seconds,
            extra_body=cfg.llm.extra_body,
        )

    if args.send_telegram:
        cfg = cfg or load_config(args.config)
        tg = TelegramSender(
            bot_token=os.environ[cfg.telegram.bot_token_env],
            chat_id=os.environ[cfg.telegram.chat_id_env],
            retry_max_attempts=cfg.retry.max_attempts,
            retry_backoff_seconds=cfg.retry.backoff_seconds,
        )

    sample_output = args.sample_output.read_text(encoding="utf-8") if args.sample_output else None
    model = args.model or (cfg.llm.model if cfg else "sample-output")
    max_tokens = args.max_tokens or (cfg.llm.max_tokens if cfg else 0)
    spec = ResearchSpec(
        experiment_id=args.experiment_id,
        date=args.date,
        fixture_paths=args.fixture,
        detail_path=args.detail_path,
        model=model,
        max_tokens=max_tokens,
        parse_mode=args.parse_mode,
        notes=args.notes,
    )
    result = run_research_once(
        spec,
        llm_client=llm,
        sample_output=sample_output,
        telegram_sender=tg,
        send_telegram=args.send_telegram,
    )
    append_result(args.results, result)
    print(
        f"{result.status}\t{result.experiment_id}\t"
        f"chunks={result.telegram_chunks}\ttokens={result.total_tokens}\t"
        f"results={args.results}"
    )
    if result.error:
        print(result.error, file=sys.stderr)
    return 0 if result.status == "ok" else 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run one chat-daily research-loop experiment and append metrics to TSV.",
    )
    parser.add_argument("--experiment-id", required=True)
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument(
        "--fixture",
        action="append",
        type=Path,
        default=[],
        help="WeChat export fixture markdown. Repeat for multiple groups.",
    )
    parser.add_argument(
        "--detail-path",
        default="output/research/summary.md",
        help="Path string inserted into the concise summary prompt.",
    )
    parser.add_argument(
        "--parse-mode",
        choices=["HTML", "MarkdownV2", "none"],
        default="HTML",
        help="Telegram formatting mode to evaluate.",
    )
    parser.add_argument("--model", default=None)
    parser.add_argument("--max-tokens", type=int, default=None)
    parser.add_argument(
        "--results",
        type=Path,
        default=Path("output/research/results.tsv"),
    )
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument(
        "--sample-output",
        type=Path,
        default=None,
        help="Use a saved LLM response instead of calling the model.",
    )
    parser.add_argument(
        "--send-telegram",
        action="store_true",
        help="Actually send to Telegram. Default is dry-run rendering only.",
    )
    parser.add_argument("--notes", default="")
    args = parser.parse_args()
    if not args.fixture:
        args.fixture = [Path("tests/fixtures/wx_export_raw_sample.md")]
    return args


if __name__ == "__main__":
    raise SystemExit(main())
