"""Entry point for chat-daily-tg. Run once per day at 08:00 local time."""
from __future__ import annotations
import argparse
from datetime import date, timedelta
import json
import logging
import os
import sys

from chat_daily_tg.archive import safe_filename, prepare_archive_day
from chat_daily_tg.config import load_config
from chat_daily_tg.env import load_env_file
from chat_daily_tg.llm_client import LLMClient
from chat_daily_tg.logging_setup import configure_logging
from chat_daily_tg.media import media_markdown, write_media_candidates
from chat_daily_tg.notifier import notify_failure
from chat_daily_tg.paths import CONFIG_PATH, DATA_DIR, log_file_for
from chat_daily_tg.summarizer import run_summary
from chat_daily_tg.tg_sender import TelegramSender
from chat_daily_tg.vision import VisionClient, analyze_media_candidates, vision_markdown, write_vision_analyses
from chat_daily_tg.wx_exporter import export_group
from chat_daily_tg.telegram_exporter import export_chat
from chat_daily_tg.sanitize import sanitize_for_llm
from chat_daily_tg.cross_group_cluster import (
    cluster_cross_group_topics,
    build_cluster_context,
    validate_clusters_in_output,
)
from chat_daily_tg.evidence_index import (
    GeminiEmbedder,
    build_evidence_context_for_summary,
    build_evidence_index,
)
from chat_daily_tg.post_process import post_process_concise

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
        notify_failure("chat-daily-tg 失败", f"{type(e).__name__}: {e}\n日志: {log_file_for(date_str)}")
        return 1


def _run(date_str: str) -> int:
    next_day = (date.fromisoformat(date_str) + timedelta(days=1)).isoformat()
    load_env_file(DATA_DIR / ".env")
    cfg = load_config(CONFIG_PATH)
    log.info(
        "config loaded: wechat=%d telegram=%d model=%s",
        len(cfg.sources.wechat.groups),
        len(cfg.sources.telegram.chats) if cfg.sources.telegram.enabled else 0,
        cfg.models.summary.model,
    )

    archive_dir = prepare_archive_day(date_str)
    groups_with_content: list[tuple[str, str]] = []
    media_candidates = []
    for group in cfg.sources.wechat.groups:
        out_path = archive_dir / f"wechat-{safe_filename(group)}.md"
        try:
            result = export_group(
                group_name=group, since=date_str, until=next_day, out_path=out_path,
            )
            log.info("exported wechat %s: %d msgs", group, result.message_count)
        except Exception as e:
            log.warning("wechat export failed for %s: %s", group, e)
            continue
        if result.content.strip():
            content = sanitize_for_llm(result.content) if cfg.sanitize.enabled else result.content
            groups_with_content.append((f"微信 / {group}", content))
        media_candidates.extend(getattr(result, "media_candidates", None) or [])

    if cfg.sources.telegram.enabled:
        for chat in cfg.sources.telegram.chats:
            out_path = archive_dir / f"telegram-{safe_filename(chat.name)}.md"
            try:
                result = export_chat(
                    chat_id=chat.id,
                    chat_name=chat.name,
                    since=date_str,
                    until=next_day,
                    out_path=out_path,
                    db_path=cfg.sources.telegram.db_path,
                    limit=chat.limit,
                    sync_before_export=cfg.sources.telegram.sync_before_export,
                )
                log.info(
                    "exported telegram %s: %d msgs, skipped=%d",
                    chat.name,
                    result.message_count,
                    result.skipped_count,
                )
            except Exception as e:
                log.warning("telegram export failed for %s: %s", chat.name, e)
                continue
            if result.content.strip():
                content = sanitize_for_llm(result.content) if cfg.sanitize.enabled else result.content
                groups_with_content.append((f"Telegram / {chat.name}", content))
            media_candidates.extend(getattr(result, "media_candidates", None) or [])

    if not groups_with_content:
        log.error("no content exported, aborting")
        return 1

    write_media_candidates(archive_dir / "media_candidates.jsonl", media_candidates)
    (archive_dir / "media_candidates.md").write_text(media_markdown(media_candidates), encoding="utf-8")
    log.info("media candidates: %d", len(media_candidates))

    if cfg.models.vision and cfg.models.vision.enabled:
        try:
            vision_api_key = os.environ[cfg.models.vision.api_key_env]
            vision_client = VisionClient(
                endpoint=cfg.models.vision.endpoint,
                model=cfg.models.vision.model,
                api_key=vision_api_key,
                timeout=cfg.models.vision.timeout,
            )
            analyses = analyze_media_candidates(client=vision_client, candidates=media_candidates)
            write_vision_analyses(archive_dir / "vision.jsonl", analyses)
            vision_md = vision_markdown(analyses)
            (archive_dir / "vision.md").write_text(vision_md, encoding="utf-8")
            if vision_md.strip():
                groups_with_content.append(("图片理解 / 多来源", vision_md))
            log.info("vision analyses included: %d", len(analyses))
        except Exception as e:
            log.warning("vision analysis skipped: %s", e)

    summary_model = cfg.models.summary
    api_key = os.environ[summary_model.api_key_env]
    llm = LLMClient(
        endpoint=summary_model.endpoint, model=summary_model.model, api_key=api_key,
        max_tokens=summary_model.max_tokens, timeout=summary_model.timeout,
        retry_max_attempts=cfg.retry.max_attempts,
        retry_backoff_seconds=cfg.retry.backoff_seconds,
        extra_body=summary_model.extra_body,
    )
    detail_path = str(archive_dir / "summary.md")
    from chat_daily_tg.context_builder import (
        active_permanent_summary, active_hot_leads_summary, active_repeat_topics_summary,
    )
    from chat_daily_tg.paths import PERMANENT_JSONL, HOT_LEADS_DIR, REPEAT_TOPICS_JSONL

    perm_ctx = active_permanent_summary(PERMANENT_JSONL)
    hot_ctx = active_hot_leads_summary(
        HOT_LEADS_DIR, retention_days=cfg.hot_leads.retention_days,
    )
    repeat_ctx = active_repeat_topics_summary(REPEAT_TOPICS_JSONL, today=date_str)

    # Cross-group clustering (preprocessing)
    clusters = cluster_cross_group_topics(groups_with_content)
    cluster_text = build_cluster_context(clusters)
    log.info("cross-group clusters: %d total (%d cross-group)",
             len(clusters), sum(1 for c in clusters if c.is_cross_group))

    evidence_context_builder = None
    evidence_index = None
    embedding_model = cfg.models.embedding if cfg.models else None
    if embedding_model and embedding_model.enabled:
        try:
            embedding_api_key = os.environ[embedding_model.api_key_env]
            embedder = GeminiEmbedder(
                endpoint=embedding_model.endpoint,
                model=embedding_model.model,
                api_key=embedding_api_key,
                timeout=embedding_model.timeout,
            )
            evidence_index = build_evidence_index(
                index_path=archive_dir / "evidence.sqlite",
                groups_with_content=groups_with_content,
                embedder=embedder,
            )

            def evidence_context_builder(summary_output):
                context = build_evidence_context_for_summary(
                    index=evidence_index,
                    embedder=embedder,
                    summary_text=summary_output.concise_md,
                    top_k=embedding_model.top_k,
                    min_similarity=embedding_model.min_similarity,
                )
                (archive_dir / "evidence-context.md").write_text(context, encoding="utf-8")
                log.info("embedding evidence context built: %d chars", len(context))
                return context
        except Exception as e:
            log.warning("embedding evidence index skipped: %s", e)

    log.info("LLM context: permanent=%d chars, hot_leads=%d chars, repeat=%d chars, clusters=%d chars, embedding=%s",
             len(perm_ctx), len(hot_ctx), len(repeat_ctx), len(cluster_text), bool(evidence_context_builder))

    log.info("calling LLM for summary…")
    try:
        out = run_summary(
            llm_client=llm, date=date_str,
            groups_with_content=groups_with_content, detail_path=detail_path,
            active_permanent_summary=perm_ctx,
            active_hot_leads_summary=hot_ctx,
            active_repeat_topics_summary=repeat_ctx,
            cross_group_cluster_text=cluster_text,
            evidence_context_builder=evidence_context_builder,
        )
    finally:
        if evidence_index is not None:
            evidence_index.close()

    # Post-hoc validation
    warnings = validate_clusters_in_output(clusters, out.concise_md)
    for w in warnings:
        log.warning("cluster validation: %s", w)
    log.info("LLM returned: concise=%d chars, detailed=%d chars",
             len(out.concise_md), len(out.detailed_md))

    (archive_dir / "concise.md").write_text(out.concise_md, encoding="utf-8")
    (archive_dir / "summary.md").write_text(out.detailed_md, encoding="utf-8")
    if out.verification is not None:
        (archive_dir / "verification.json").write_text(
            json.dumps(out.verification, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    concise_processed = post_process_concise(out.concise_md, cfg.source_abbreviations)

    # Guard against empty or near-empty output after repair
    if len(concise_processed.strip()) < 100:
        log.error("concise output too short (%d chars), skipping TG push", len(concise_processed.strip()))
        notify_failure("chat-daily-tg 日报生成异常", f"精简版输出过短（{len(concise_processed.strip())} 字符），可能 LLM 格式解析失败。日志: {log_file_for(date_str)}")
        return 1

    # 4.5. Persist opportunities
    from datetime import datetime as _dt
    from chat_daily_tg.db import PermanentDB, PermanentEntry
    from chat_daily_tg.hot_leads import HotLead, append_day_leads, regenerate_latest
    from chat_daily_tg.permanent_md import regenerate_permanent_md
    from chat_daily_tg.paths import (
        PERMANENT_JSONL, PERMANENT_MD, REPEAT_TOPICS_JSONL, HOT_LEADS_DIR, HOT_LEADS_LATEST,
    )
    from chat_daily_tg.repeat_topics import RepeatTopicDB, mentions_from_json

    from chat_daily_tg.db import compute_fingerprint
    pdb = PermanentDB(PERMANENT_JSONL)
    now_iso = _dt.now().isoformat()
    candidates: list[PermanentEntry] = []
    for add in out.opportunities.get("permanent_additions", []):
        title = add.get("title", "")
        url = add.get("url")
        category = add.get("category", "misc")
        fp = compute_fingerprint(title, url, category)
        candidates.append(PermanentEntry(
            id=f"{date_str}-{fp[:8]}",
            captured_at=now_iso,
            source_group=add.get("source_group", ""),
            source_sender=add.get("source_sender", ""),
            category=category,
            type=add.get("type", "permanent"),
            title=title,
            content=add.get("content", ""),
            url=url,
            expires_at=add.get("expires_at"),
            notes=add.get("notes"),
        ))
    for action, saved in pdb.upsert_many(candidates):
        log.info("permanent %s (mention=%d): %s", action, saved.mention_count, saved.title)

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

    topic_mentions = mentions_from_json(out.opportunities.get("topic_mentions", []))
    repeat_updated = RepeatTopicDB(REPEAT_TOPICS_JSONL).upsert_many(topic_mentions, seen_date=date_str)
    log.info("repeat topics updated: %d", len(repeat_updated))

    from chat_daily_tg.death_signals import apply_death_signals as _apply_ds
    n_updated = _apply_ds(
        signals=out.opportunities.get("death_signals", []),
        db_path=PERMANENT_JSONL,
        hot_leads_root=HOT_LEADS_DIR,
    )
    log.info("death signals applied: %d", n_updated)

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
    tg.send(concise_processed, parse_mode="HTML")
    log.info("TG push complete")

    log.info("✓ run_daily complete for %s", date_str)
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--date", help="YYYY-MM-DD (default: yesterday)", default=None)
    args = p.parse_args()
    sys.exit(main(date_str=args.date))
