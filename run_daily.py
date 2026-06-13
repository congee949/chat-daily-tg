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


# Written to the archive dir after a fully successful pushed run. The launchd
# catch-up intervals re-invoke run_daily with --skip-if-done, so a morning run
# that failed (or slept through) is retried later the same day, while a
# completed run makes the catch-ups no-ops.
COMPLETE_MARKER = ".run-complete"


def completion_marker(date_str: str):
    return prepare_archive_day(date_str) / COMPLETE_MARKER


def main(date_str: str | None = None, model_alias: str | None = None, no_push: bool = False,
         skip_if_done: bool = False) -> int:
    if date_str is None:
        date_str = yesterday_iso()
    configure_logging(log_file_for(date_str))
    if skip_if_done and completion_marker(date_str).exists():
        log.info("run for %s already complete, skipping (--skip-if-done)", date_str)
        return 0
    try:
        return _run(date_str, model_alias=model_alias, no_push=no_push)
    except Exception as e:
        log.exception("pipeline failed: %s", e)
        notify_failure("chat-daily-tg 失败", f"{type(e).__name__}: {e}\n日志: {log_file_for(date_str)}")
        return 1


def _append_evidence_context(detailed_md: str, evidence_context: str) -> str:
    if not evidence_context.strip():
        return detailed_md
    return f"{detailed_md.rstrip()}\n\n## Embedding 检索证据\n\n{evidence_context.strip()}\n"


def _push_raw_channels(cfg, since, until, archive_dir, *, no_push: bool, incremental: bool) -> None:
    """Verbatim channel-card stage. Builds its own bot sender and is fully isolated —
    any failure only logs/notifies. Runs as a standalone 2-hourly forwarder
    (incremental=True): each channel fetches only messages newer than its high-water
    mark, so high-volume private channels aren't re-downloaded."""
    channels = cfg.sources.telegram.raw_channels if cfg.sources.telegram.enabled else []
    if not channels:
        return
    try:
        from chat_daily_tg.raw_channels import push_raw_channel_cards
        from chat_daily_tg.tg_sender import TelegramSender
        from chat_daily_tg.paths import DATA_DIR
        sender = None
        if not no_push:
            sender = TelegramSender(
                bot_token=os.environ[cfg.telegram.bot_token_env],
                chat_id=os.environ[cfg.telegram.chat_id_env],
                retry_max_attempts=cfg.retry.max_attempts,
                retry_backoff_seconds=cfg.retry.backoff_seconds,
            )
        n = push_raw_channel_cards(
            channels=channels,
            since=since,
            until=until,
            db_path=cfg.sources.telegram.db_path,
            sender=sender,
            archive_dir=archive_dir,
            seen_path=DATA_DIR / "raw_channel_seen.txt",
            sync_before_export=cfg.sources.telegram.sync_before_export,
            delay_seconds=cfg.sources.telegram.raw_card_delay_seconds,
            no_push=no_push,
            incremental=incremental,
        )
        log.info("raw channel cards pushed: %d (incremental=%s, no_push=%s)", n, incremental, no_push)
    except Exception as e:
        log.exception("raw channel stage failed: %s", e)
        notify_failure("chat-daily-tg 频道原文卡片失败", f"{type(e).__name__}: {e}")


def run_channels(no_push: bool = False) -> int:
    """Entry point for the 2-hourly channel forwarder (--channels-only). Pushes only
    verbatim channel cards, incrementally; does NOT run the daily LLM summary. The
    window spans [yesterday, tomorrow) as a safety bound — the per-channel high-water
    mark does the real filtering so nothing is re-pushed or re-downloaded."""
    today = date.today()
    tag = today.isoformat()
    configure_logging(log_file_for(f"channels-{tag}"))
    try:
        load_env_file(DATA_DIR / ".env")
        cfg = load_config(CONFIG_PATH)
        archive_dir = prepare_archive_day(tag)
        since = (today - timedelta(days=1)).isoformat()
        until = (today + timedelta(days=1)).isoformat()
        _push_raw_channels(cfg, since, until, archive_dir, no_push=no_push, incremental=True)
        return 0
    except Exception as e:
        log.exception("channels forwarder failed: %s", e)
        notify_failure("chat-daily-tg 频道转发失败", f"{type(e).__name__}: {e}")
        return 1


def _fact_risk_report(verification: dict) -> str:
    risky_statuses = {"downgraded", "removed", "needs_verification"}
    risky_claims = [
        claim for claim in verification.get("checked_claims", [])
        if isinstance(claim, dict) and claim.get("status") in risky_statuses
    ]
    if not risky_claims:
        return ""
    lines = ["# 事实风险报告", "", "Verifier 标记了以下需要人工关注的 claim。", ""]
    for claim in risky_claims:
        lines.append(f"## {claim.get('claim', '(未命名 claim)')}")
        lines.append("")
        lines.append(f"- status: {claim.get('status', '')}")
        lines.append(f"- confidence: {claim.get('confidence', '')}")
        lines.append(f"- reason: {claim.get('reason', '')}")
        evidence = claim.get("evidence") or []
        if evidence:
            lines.append("- evidence:")
            for item in evidence:
                lines.append(f"  - {item}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _run(date_str: str, *, model_alias: str | None = None, no_push: bool = False) -> int:
    next_day = (date.fromisoformat(date_str) + timedelta(days=1)).isoformat()
    load_env_file(DATA_DIR / ".env")
    cfg = load_config(CONFIG_PATH)
    if model_alias:
        cfg.override_summary_model(model_alias)
        log.info("model overridden to: %s (%s)", model_alias, cfg.models.summary.model)
    log.info(
        "config loaded: wechat=%d telegram=%d model=%s",
        len(cfg.sources.wechat.groups),
        len(cfg.sources.telegram.chats) if cfg.sources.telegram.enabled else 0,
        cfg.models.summary.model,
    )
    if not cfg.sources.wechat.groups and not (
        cfg.sources.telegram.enabled and cfg.sources.telegram.chats
    ):
        # Config validation accepts a raw_channels-only config (valid for the
        # --channels-only forwarder), but the daily summary doesn't consume raw
        # channels — fail fast with a precise message instead of exporting nothing
        # and dying later with "no content exported".
        log.error("no daily-summary sources configured — raw_channels only feed the "
                  "--channels-only forwarder; add sources.wechat.groups or sources.telegram.chats")
        return 1

    archive_dir = prepare_archive_day(date_str)
    # Channel cards are handled by the separate 2-hourly forwarder (run_channels), not
    # by this daily summary run.
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
            if cfg.models.vision and cfg.models.vision.enabled:
                # tg-cli's messages.db carries no media — pull this chat's photos via
                # telethon (image-only side path) so the vision pipeline below can see
                # them. Failure-isolated: returns [] on any error, text export stands.
                from chat_daily_tg.telegram_media import export_chat_media
                media_candidates.extend(export_chat_media(
                    chat_id=chat.id, chat_name=chat.name,
                    since=date_str, until=next_day,
                    out_dir=archive_dir, limit=chat.limit,
                ))

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
    evidence_context_for_archive = ""
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
                output_dimensionality=embedding_model.dimension,
            )
            evidence_index = build_evidence_index(
                index_path=archive_dir / "evidence.sqlite",
                groups_with_content=groups_with_content,
                embedder=embedder,
            )

            def evidence_context_builder(summary_output):
                nonlocal evidence_context_for_archive
                context = build_evidence_context_for_summary(
                    index=evidence_index,
                    embedder=embedder,
                    summary_text=f"{summary_output.concise_md}\n\n{summary_output.detailed_md}",
                    top_k=embedding_model.top_k,
                    min_similarity=embedding_model.min_similarity,
                )
                evidence_context_for_archive = context
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
    detailed_archive = _append_evidence_context(out.detailed_md, evidence_context_for_archive)
    (archive_dir / "summary.md").write_text(detailed_archive, encoding="utf-8")
    if out.verification is not None:
        (archive_dir / "verification.json").write_text(
            json.dumps(out.verification, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        risk_report = _fact_risk_report(out.verification)
        if risk_report:
            (archive_dir / "fact-risk-report.md").write_text(risk_report, encoding="utf-8")
            log.warning("fact risk report generated: %s", archive_dir / "fact-risk-report.md")

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

    if not no_push:
        bot_token = os.environ[cfg.telegram.bot_token_env]
        chat_id = os.environ[cfg.telegram.chat_id_env]
        tg = TelegramSender(
            bot_token=bot_token, chat_id=chat_id,
            retry_max_attempts=cfg.retry.max_attempts,
            retry_backoff_seconds=cfg.retry.backoff_seconds,
        )
        image_sent = False
        if cfg.telegram.send_image:
            # Render a glanceable PNG card and send it BEFORE the text. Any failure
            # (render/Chrome/sendPhoto) only logs and falls through to the text push.
            try:
                from chat_daily_tg.card_renderer import (
                    card_caption, parse_concise_to_card, render_card_png,
                )
                card = parse_concise_to_card(out.concise_md, date_str)
                png = render_card_png(card, archive_dir / "card.png")
                if png:
                    caption = card_caption(card) if cfg.telegram.image_caption else ""
                    tg.send_photo(png, caption=caption)
                    image_sent = True
                    log.info("TG card image sent")
            except Exception as e:
                log.warning("card image push failed, falling back to text: %s", e)
        if image_sent and cfg.telegram.image_only:
            # Image-only mode: the card was delivered, so skip the full text message.
            # (Text still sends below if the image failed — image_sent would be False.)
            log.info("image_only mode: skipping text push")
        else:
            tg.send(concise_processed, parse_mode="HTML")
            log.info("TG push complete")
    else:
        log.info("TG push skipped (--no-push)")

    if not no_push:
        # Only a pushed run counts as delivered — a --no-push debug run must not
        # suppress the same-day catch-up retries.
        (archive_dir / COMPLETE_MARKER).write_text(_dt.now().isoformat(), encoding="utf-8")
    log.info("✓ run_daily complete for %s", date_str)
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--date", help="YYYY-MM-DD (default: yesterday)", default=None)
    p.add_argument("--model", help="Model alias from config (e.g. 'gemini')", default=None)
    p.add_argument("--no-push", action="store_true", help="Skip Telegram push")
    p.add_argument("--skip-if-done", action="store_true",
                   help="Exit 0 immediately if this date's run already completed (catch-up schedule)")
    p.add_argument("--channels-only", action="store_true",
                   help="Run only the 2-hourly verbatim channel forwarder (no summary)")
    args = p.parse_args()
    if args.channels_only:
        sys.exit(run_channels(no_push=args.no_push))
    sys.exit(main(date_str=args.date, model_alias=args.model, no_push=args.no_push,
                  skip_if_done=args.skip_if_done))
