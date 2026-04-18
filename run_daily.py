"""Entry point for wx-daily-tg. Run once per day at 08:00 local time."""
from __future__ import annotations
import argparse
from datetime import date, timedelta
import os
import sys

from wx_daily_tg.archive import safe_filename, prepare_archive_day
from wx_daily_tg.config import load_config
from wx_daily_tg.llm_client import LLMClient
from wx_daily_tg.paths import CONFIG_PATH
from wx_daily_tg.summarizer import run_summary
from wx_daily_tg.tg_sender import TelegramSender


def yesterday_iso() -> str:
    return (date.today() - timedelta(days=1)).isoformat()


def main(date_str: str | None = None) -> int:
    if date_str is None:
        date_str = yesterday_iso()
    next_day = (date.fromisoformat(date_str) + timedelta(days=1)).isoformat()

    cfg = load_config(CONFIG_PATH)

    # 1. Export each group
    archive_dir = prepare_archive_day(date_str)
    groups_with_content: list[tuple[str, str]] = []
    for group in cfg.groups:
        out_path = archive_dir / f"{safe_filename(group)}.md"
        from wx_daily_tg.wx_exporter import export_group
        try:
            result = export_group(
                group_name=group, since=date_str, until=next_day, out_path=out_path,
            )
            print(f"[export] {group}: {result.message_count} msgs → {out_path}")
        except Exception as e:
            print(f"[export][WARN] {group}: {e}", file=sys.stderr)
            continue
        content = out_path.read_text(encoding="utf-8")
        if content.strip():
            groups_with_content.append((group, content))

    if not groups_with_content:
        print("[run_daily] no content exported, aborting", file=sys.stderr)
        return 1

    # 2. LLM summarize
    api_key = os.environ[cfg.llm.api_key_env]
    llm = LLMClient(
        endpoint=cfg.llm.endpoint,
        model=cfg.llm.model,
        api_key=api_key,
        max_tokens=cfg.llm.max_tokens,
    )
    detail_path = str(archive_dir / "summary.md")
    out = run_summary(
        llm_client=llm,
        date=date_str,
        groups_with_content=groups_with_content,
        detail_path=detail_path,
    )

    # 3. Write detailed archive
    (archive_dir / "summary.md").write_text(out.detailed_md, encoding="utf-8")

    # 4. Push Telegram
    bot_token = os.environ[cfg.telegram.bot_token_env]
    chat_id = os.environ[cfg.telegram.chat_id_env]
    tg = TelegramSender(bot_token=bot_token, chat_id=chat_id)
    tg.send(out.concise_md)

    print(f"[run_daily] ✓ complete for {date_str}")
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--date", help="YYYY-MM-DD (default: yesterday)", default=None)
    args = p.parse_args()
    sys.exit(main(date_str=args.date))
