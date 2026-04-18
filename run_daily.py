"""Entry point for wx-daily-tg. Run once per day at 08:00 local time."""
from __future__ import annotations
import argparse
from datetime import date, timedelta
import logging
import os
import sys

from wx_daily_tg.archive import safe_filename, prepare_archive_day
from wx_daily_tg.config import load_config
from wx_daily_tg.llm_client import LLMClient
from wx_daily_tg.logging_setup import configure_logging
from wx_daily_tg.notifier import notify_failure
from wx_daily_tg.paths import CONFIG_PATH, log_file_for
from wx_daily_tg.summarizer import run_summary
from wx_daily_tg.tg_sender import TelegramSender
from wx_daily_tg.wx_exporter import export_group

log = logging.getLogger("run_daily")


def yesterday_iso() -> str:
    return (date.today() - timedelta(days=1)).isoformat()


def main(date_str: str | None = None) -> int:
    if date_str is None:
        date_str = yesterday_iso()
    configure_logging(log_file_for(date_str))
    try:
        return _run(date_str)
    except Exception as e:
        log.exception("pipeline failed: %s", e)
        notify_failure("wx-daily-tg 失败", f"{type(e).__name__}: {e}\n日志: {log_file_for(date_str)}")
        return 1


def _run(date_str: str) -> int:
    next_day = (date.fromisoformat(date_str) + timedelta(days=1)).isoformat()
    cfg = load_config(CONFIG_PATH)
    log.info("config loaded: %d groups, model=%s", len(cfg.groups), cfg.llm.model)

    archive_dir = prepare_archive_day(date_str)
    groups_with_content: list[tuple[str, str]] = []
    for group in cfg.groups:
        out_path = archive_dir / f"{safe_filename(group)}.md"
        try:
            result = export_group(
                group_name=group, since=date_str, until=next_day, out_path=out_path,
            )
            log.info("exported %s: %d msgs", group, result.message_count)
        except Exception as e:
            log.warning("export failed for %s: %s", group, e)
            continue
        content = out_path.read_text(encoding="utf-8")
        if content.strip():
            groups_with_content.append((group, content))

    if not groups_with_content:
        log.error("no content exported, aborting")
        return 1

    api_key = os.environ[cfg.llm.api_key_env]
    llm = LLMClient(
        endpoint=cfg.llm.endpoint, model=cfg.llm.model, api_key=api_key,
        max_tokens=cfg.llm.max_tokens, timeout=cfg.llm.timeout,
        retry_max_attempts=cfg.retry.max_attempts,
        retry_backoff_seconds=cfg.retry.backoff_seconds,
    )
    detail_path = str(archive_dir / "summary.md")
    from wx_daily_tg.context_builder import (
        active_permanent_summary, active_hot_leads_summary,
    )
    from wx_daily_tg.paths import PERMANENT_JSONL, HOT_LEADS_DIR

    perm_ctx = active_permanent_summary(PERMANENT_JSONL)
    hot_ctx = active_hot_leads_summary(
        HOT_LEADS_DIR, retention_days=cfg.hot_leads.retention_days,
    )
    log.info("LLM context: permanent=%d chars, hot_leads=%d chars",
             len(perm_ctx), len(hot_ctx))

    log.info("calling LLM for summary…")
    out = run_summary(
        llm_client=llm, date=date_str,
        groups_with_content=groups_with_content, detail_path=detail_path,
        active_permanent_summary=perm_ctx,
        active_hot_leads_summary=hot_ctx,
    )
    log.info("LLM returned: concise=%d chars, detailed=%d chars",
             len(out.concise_md), len(out.detailed_md))

    (archive_dir / "summary.md").write_text(out.detailed_md, encoding="utf-8")

    # 4.5. Persist opportunities
    from datetime import datetime as _dt
    from wx_daily_tg.db import PermanentDB, PermanentEntry
    from wx_daily_tg.hot_leads import HotLead, append_day_leads, regenerate_latest
    from wx_daily_tg.permanent_md import regenerate_permanent_md
    from wx_daily_tg.paths import (
        PERMANENT_JSONL, PERMANENT_MD, HOT_LEADS_DIR, HOT_LEADS_LATEST,
    )

    pdb = PermanentDB(PERMANENT_JSONL)
    for i, add in enumerate(out.opportunities.get("permanent_additions", [])):
        entry = PermanentEntry(
            id=f"{date_str}-perm-{i:03d}",
            captured_at=_dt.now().isoformat(),
            source_group=add.get("source_group", ""),
            source_sender=add.get("source_sender", ""),
            category=add.get("category", "misc"),
            type=add.get("type", "permanent"),
            title=add.get("title", ""),
            content=add.get("content", ""),
            url=add.get("url"),
            expires_at=add.get("expires_at"),
            notes=add.get("notes"),
        )
        pdb.append(entry)
        log.info("permanent add: %s", entry.title)

    hot_leads_new: list[HotLead] = []
    for i, add in enumerate(out.opportunities.get("hot_leads_additions", [])):
        lead = HotLead(
            id=f"{date_str}-hot-{i:03d}",
            captured_at=date_str,
            title=add.get("title", ""),
            summary=add.get("summary", ""),
            category=add.get("category", "arbitrage"),
            source_group=add.get("source_group", ""),
            source_sender=add.get("source_sender", ""),
            status="alive",
            risk_notes=add.get("risk_notes"),
        )
        hot_leads_new.append(lead)
    append_day_leads(HOT_LEADS_DIR, date_str, hot_leads_new)
    log.info("hot leads added: %d", len(hot_leads_new))

    # Regenerate derived views
    regenerate_permanent_md(PERMANENT_JSONL, PERMANENT_MD)
    regenerate_latest(HOT_LEADS_DIR, HOT_LEADS_LATEST, retention_days=cfg.hot_leads.retention_days)

    bot_token = os.environ[cfg.telegram.bot_token_env]
    chat_id = os.environ[cfg.telegram.chat_id_env]
    tg = TelegramSender(
        bot_token=bot_token, chat_id=chat_id,
        retry_max_attempts=cfg.retry.max_attempts,
        retry_backoff_seconds=cfg.retry.backoff_seconds,
    )
    tg.send(out.concise_md)
    log.info("TG push complete")

    log.info("✓ run_daily complete for %s", date_str)
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--date", help="YYYY-MM-DD (default: yesterday)", default=None)
    args = p.parse_args()
    sys.exit(main(date_str=args.date))
