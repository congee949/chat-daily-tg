"""Entry point for chat-daily-tg.

Serves all four pipelines; flags pick one (see --help). Scheduling lives in the launchd
plists, not here — the daily summary fires 07:05; --wait-for-wake probes once for the
Watch sleep episode (use real wake time when synced; if no sleep data, deliver now). The `schedule`
block is scheduling-dead (launchd owns timing), but `schedule.timezone` IS read
(health briefing day-boundary + wake deadline) — do not delete the block.
"""
from __future__ import annotations
import argparse
from dataclasses import replace
from datetime import date, timedelta
import json
import logging
import os
from pathlib import Path
import re
import signal
import sys

from chat_daily_tg.archive import safe_filename, prepare_archive_day, cleanup_old_media
from chat_daily_tg.config import load_config
from chat_daily_tg.env import load_env_file, scrub_socks_proxy_env
from chat_daily_tg.llm_client import LLMClient
from chat_daily_tg.logging_setup import configure_logging
from chat_daily_tg.media import media_markdown, write_media_candidates
from chat_daily_tg.notifier import notify_failure
from chat_daily_tg.paths import CONFIG_PATH, DATA_DIR, log_file_for
from chat_daily_tg.summarizer import run_summary
from chat_daily_tg.tg_sender import TelegramSender
from chat_daily_tg.vision import (
    VisionClient, analyze_media_candidates, build_citation_block, resolve_citations,
    vision_markdown, vision_zero_image_failure, write_vision_analyses,
    write_vision_audit,
)
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
from chat_daily_tg.post_process import abbreviate_sources, post_process_concise

log = logging.getLogger("run_daily")


def yesterday_iso() -> str:
    return (date.today() - timedelta(days=1)).isoformat()


TG_TARGETS = os.path.expanduser("~/qwenproxy/.tg-notify-targets.json")


def resolve_tg_target(topic_key: str, dm_chat_id: str) -> tuple[str, int | None]:
    """Resolve (chat_id, message_thread_id) for a forum topic from the shared route
    table. Falls back to the DM chat with no thread — 'rather deliver to DM than
    drop' — but surfaces WHY it fell back via notify_failure, so a missing/corrupt
    table or an unregistered topic key (previously swallowed by a bare except and
    silently misrouted to DM) becomes visible. Mirrors
    ~/qwenproxy/session-expiry-notify.py::send_tg()."""
    reason: str
    try:
        with open(TG_TARGETS) as f:
            t = json.load(f)
    except FileNotFoundError:
        reason = f"路由表缺失 ({TG_TARGETS})"
    except (json.JSONDecodeError, OSError) as e:
        reason = f"路由表不可读 ({type(e).__name__})"
    else:
        chat_id = t.get("chat_id") or dm_chat_id
        tid = (t.get("topics", {}) or {}).get(topic_key)
        if tid:  # 0/None/missing all mean "no thread" -> fall through to DM
            return str(chat_id), int(tid)
        reason = f"topic key {topic_key!r} 不在路由表"

    # Alerting must never break the push; notify_failure is itself best-effort.
    try:
        notify_failure("TG 路由回落 DM", f"{reason}；内容改发 DM (topic={topic_key})")
    except Exception:
        log.warning("route fallback alert failed for topic=%s", topic_key)
    return dm_chat_id, None


# Written to the archive dir after a fully successful pushed run. --skip-if-done
# (passed by the launchd wrapper) makes a re-fired trigger — launchd coalesces a
# 07:05 slept through and re-fires it on Mac wake — a no-op once the day is
# delivered, while a failed run stays retryable.
COMPLETE_MARKER = ".run-complete"
# Written once opportunities are persisted, BEFORE the (separately-retried) push.
# A same-day catch-up after a push failure then skips re-persisting — which would
# otherwise re-append hot leads under fresh non-deterministic ids (review #40).
PERSISTED_MARKER = ".persisted"


def completion_marker(date_str: str):
    return prepare_archive_day(date_str) / COMPLETE_MARKER


# Closed enums declared to the LLM in the opportunities fence (prompts.py). The
# model occasionally emits a value outside the set; coerce it to a safe default
# instead of letting an unknown category/type land in the DB unchecked.
PERMANENT_CATEGORIES = {"invite_code", "bank_product", "activity", "misc"}
PERMANENT_TYPES = {"permanent", "product", "activity"}
HOT_LEAD_CATEGORIES = {"arbitrage", "bug", "personal_trick", "gray_zone"}


def coerce_enum(value, allowed: set[str], default: str, field: str) -> str:
    """Map an LLM-emitted enum onto its closed set: empty/missing -> default
    (unchanged from the prior `or default` behaviour), a value already inside
    `allowed` passes through, and anything else is coerced to `default` with a
    warning naming the raw value (also covers non-str/unhashable junk)."""
    if not value:
        return default
    if isinstance(value, str) and value in allowed:
        return value
    log.warning("opportunity %s got out-of-enum %r; coerced to %s", field, value, default)
    return default


def _persist_opportunities(out, date_str: str, hot_leads_dir) -> None:
    """Write the run's opportunities to the shared DB (idempotent upserts)."""
    from datetime import datetime as _dt
    from chat_daily_tg.db import PermanentDB, PermanentEntry, compute_fingerprint
    from chat_daily_tg.hot_leads import HotLead, append_day_leads
    from chat_daily_tg.repeat_topics import RepeatTopicDB, mentions_from_json
    from chat_daily_tg.death_signals import apply_death_signals
    from chat_daily_tg.paths import DB_PATH

    pdb = PermanentDB(DB_PATH)
    now_iso = _dt.now().isoformat()
    candidates: list[PermanentEntry] = []
    for add in out.opportunities.get("permanent_additions", []):
        # The LLM may emit an explicit null for any field. `.get(key, default)` only
        # substitutes the default for a MISSING key, so an explicit null passes through
        # as None and trips "NOT NULL constraint failed" on the NOT NULL columns
        # (source_sender/source_group/title/category/type/content). `or default`
        # coerces null → default, matching the `.get(x) or ""` convention elsewhere.
        title = add.get("title") or ""
        url = add.get("url")
        category = coerce_enum(add.get("category"), PERMANENT_CATEGORIES, "misc", "permanent.category")
        fp = compute_fingerprint(title, url, category)
        candidates.append(PermanentEntry(
            id=f"{date_str}-{fp[:8]}",
            captured_at=now_iso,
            source_group=add.get("source_group") or "",
            source_sender=add.get("source_sender") or "",
            category=category,
            type=coerce_enum(add.get("type"), PERMANENT_TYPES, "permanent", "permanent.type"),
            title=title,
            content=add.get("content") or "",
            url=url,
            expires_at=add.get("expires_at"),
            notes=add.get("notes"),
        ))
    for action, saved in pdb.upsert_many(candidates):
        log.info("permanent %s (mention=%d): %s", action, saved.mention_count, saved.title)

    hot_leads_new: list[HotLead] = []
    for i, add in enumerate(out.opportunities.get("hot_leads_additions", [])):
        hot_leads_new.append(HotLead(
            id=f"{date_str}-hot-{i:03d}",
            captured_at=date_str,
            title=add.get("title") or "",
            summary=add.get("summary") or "",
            category=coerce_enum(add.get("category"), HOT_LEAD_CATEGORIES, "arbitrage", "hot_leads.category"),
            source_group=add.get("source_group") or "",
            source_sender=add.get("source_sender") or "",
            status="alive",
            risk_notes=add.get("risk_notes"),
        ))
    append_day_leads(DB_PATH, date_str, hot_leads_new, md_root=hot_leads_dir)
    log.info("hot leads added: %d", len(hot_leads_new))

    topic_mentions = mentions_from_json(out.opportunities.get("topic_mentions", []))
    repeat_updated = RepeatTopicDB(DB_PATH).upsert_many(topic_mentions, seen_date=date_str)
    log.info("repeat topics updated: %d", len(repeat_updated))

    n_updated = apply_death_signals(
        signals=out.opportunities.get("death_signals", []),
        db_path=DB_PATH,
        hot_leads_db=DB_PATH,
    )
    log.info("death signals applied: %d", n_updated)


def main(date_str: str | None = None, model_alias: str | None = None, no_push: bool = False,
         skip_if_done: bool = False, wait_for_wake: bool = False,
         wake_deadline: str = "13:00") -> int:
    if date_str is None:
        date_str = yesterday_iso()
    configure_logging(log_file_for(date_str))
    if skip_if_done and completion_marker(date_str).exists():
        log.info("run for %s already complete, skipping (--skip-if-done)", date_str)
        return 0
    try:
        return _run(date_str, model_alias=model_alias, no_push=no_push,
                    wait_for_wake=wait_for_wake, wake_deadline=wake_deadline)
    except Exception as e:
        log.exception("pipeline failed: %s", e)
        notify_failure("chat-daily-tg 失败", f"{type(e).__name__}: {e}\n日志: {log_file_for(date_str)}")
        return 1


def _append_evidence_context(detailed_md: str, evidence_context: str) -> str:
    if not evidence_context.strip():
        return detailed_md
    return f"{detailed_md.rstrip()}\n\n## Embedding 检索证据\n\n{evidence_context.strip()}\n"


def _build_dedup_gates(cfg, *, no_push: bool):
    """Construct the L1 content store and L2 topic gate for one channels run.
    Every failure degrades to None (= that layer off for the run, delivery
    proceeds) — dedup must never be the reason a card doesn't go out."""
    content_store = None
    topic_gate = None
    if no_push:
        return None, None
    dedup = cfg.sources.telegram.dedup
    if dedup.content.enabled:
        try:
            from chat_daily_tg.content_seen import ContentSeenStore
            from chat_daily_tg.paths import CONTENT_SEEN_DB
            content_store = ContentSeenStore(
                CONTENT_SEEN_DB, window_days=dedup.content.window_days)
        except Exception as e:
            log.warning("content dedup store unavailable (layer off this run): %s", e)
    if dedup.topic.enabled:
        try:
            from chat_daily_tg.evidence_index import GeminiEmbedder
            from chat_daily_tg.paths import DELIVERED_INDEX_DB
            from chat_daily_tg.topic_dedup import (
                DeliveredIndex, SameEventJudge, TopicDedupGate,
            )
            t = dedup.topic
            em = cfg.models.embedding if cfg.models else None
            if not (em and em.enabled):
                raise RuntimeError("models.embedding disabled — L2 needs it")
            embedder = GeminiEmbedder.from_config(em)
            index = DeliveredIndex(DELIVERED_INDEX_DB, window_days=t.index_window_days)
            # SameEventJudge applies model/timeout overrides to a replace() COPY.
            judge = SameEventJudge(
                _llm_from_block(cfg, cfg.resolve_model_alias(t.judge_model_alias)),
                model=t.judge_model, timeout=t.judge_timeout_seconds,
            )
            topic_gate = TopicDedupGate(
                index, embedder, judge, mode=t.mode,
                candidate_min_sim=t.candidate_min_sim, strong_sim=t.strong_sim,
                retrieval_window_hours=t.retrieval_window_hours,
                exclude_producers=frozenset(t.exclude_producers),
                max_judge_calls_per_run=t.max_judge_calls_per_run,
                # The annotation deep-link base and the ingest target are the
                # SAME group — derived, not restated, so a forum migration
                # can't leave 前文↗ links pointing into the dead group.
                group_internal_id=str(t.forum_chat_id).removeprefix("-100").lstrip("-"),
                # Ingest+backfill run lazily at the first prepare() with real
                # cards: a zero-new-card run costs zero network calls.
                ingest={
                    "db_path": Path(cfg.sources.telegram.db_path).expanduser(),
                    "forum_chat_id": t.forum_chat_id,
                    "sync_limit": t.sync_limit,
                },
            )
        except Exception as e:
            log.warning("topic dedup gate unavailable (layer off this run): %s", e)
    return content_store, topic_gate


def _push_raw_channels(cfg, since, until, archive_dir, *, no_push: bool, incremental: bool) -> None:
    """Verbatim channel-card stage, fully isolated — any failure only logs/notifies.
    Runs as a standalone 2-hourly forwarder (incremental=True): each channel fetches
    only messages newer than its high-water mark, so high-volume private channels
    aren't re-downloaded.

    Channels route to forum topics by their `topic` key (default channels_news).
    Each topic group gets its own sender via resolve_tg_target (group chat +
    message_thread_id). seen_path is keyed per channel, so splitting the single
    push into per-topic calls causes no cross-group dup/skip."""
    channels = cfg.sources.telegram.raw_channels if cfg.sources.telegram.enabled else []
    if not channels:
        return
    try:
        from collections import OrderedDict
        from chat_daily_tg.raw_channels import push_raw_channel_cards
        from chat_daily_tg.tg_sender import TelegramSender
        from chat_daily_tg.paths import DATA_DIR
        dm_chat_id = os.environ[cfg.telegram.chat_id_env]
        bot_token = os.environ[cfg.telegram.bot_token_env]
        seen_path = DATA_DIR / "raw_channel_seen.txt"
        # One store/gate pair per run (the L2 judge budget is global across
        # channels); construction failure = that layer off, delivery proceeds.
        content_store, topic_gate = _build_dedup_gates(cfg, no_push=no_push)
        groups: "OrderedDict[str, list]" = OrderedDict()
        for ch in channels:
            groups.setdefault(ch.topic or "channels_news", []).append(ch)
        total = 0
        for topic_key, chs in groups.items():
            sender = None
            target = "(no_push)"
            if not no_push:
                chat_id, thread_id = resolve_tg_target(topic_key, dm_chat_id)
                target = f"{chat_id}/thread={thread_id}"
                sender = TelegramSender(
                    bot_token=bot_token,
                    chat_id=chat_id,
                    message_thread_id=thread_id,
                    retry_max_attempts=cfg.retry.max_attempts,
                    retry_backoff_seconds=cfg.retry.backoff_seconds,
                )
            n = push_raw_channel_cards(
                channels=chs,
                since=since,
                until=until,
                db_path=cfg.sources.telegram.db_path,
                sender=sender,
                archive_dir=archive_dir,
                seen_path=seen_path,
                sync_before_export=cfg.sources.telegram.sync_before_export,
                delay_seconds=cfg.sources.telegram.raw_card_delay_seconds,
                no_push=no_push,
                incremental=incremental,
                content_store=content_store,
                topic_gate=topic_gate,
            )
            total += n
            log.info("raw channel cards pushed: %d -> topic=%s %s", n, topic_key, target)
        log.info("raw channel cards pushed total: %d (incremental=%s, no_push=%s)", total, incremental, no_push)
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


def run_resend(spec: str) -> int:
    """--resend CHAT_ID:MSG_ID — rebuild and deliver one channel card, bypassing
    SeenStore, the high-water mark and every dedup layer. The recovery hatch for
    a wrong suppression: the dedup journal / rawcard archive carry the ids."""
    configure_logging(log_file_for(f"resend-{date.today().isoformat()}"))
    try:
        chat_id, _, msg_id_s = spec.partition(":")
        msg_id = int(msg_id_s)
        load_env_file(DATA_DIR / ".env")
        cfg = load_config(CONFIG_PATH)
        channel = next(
            (c for c in cfg.sources.telegram.raw_channels if c.id == chat_id), None)
        if channel is None:
            log.error("resend: chat_id %s is not a configured raw channel", chat_id)
            return 1
        from chat_daily_tg.paths import DATA_DIR as _dd
        from chat_daily_tg.raw_channels import resend_raw_card
        from chat_daily_tg.tg_sender import TelegramSender
        dm_chat_id = os.environ[cfg.telegram.chat_id_env]
        tg_chat_id, thread_id = resolve_tg_target(
            channel.topic or "channels_news", dm_chat_id)
        sender = TelegramSender(
            bot_token=os.environ[cfg.telegram.bot_token_env],
            chat_id=tg_chat_id, message_thread_id=thread_id,
            retry_max_attempts=cfg.retry.max_attempts,
            retry_backoff_seconds=cfg.retry.backoff_seconds,
        )
        ok = resend_raw_card(
            channel=channel, msg_id=msg_id,
            db_path=cfg.sources.telegram.db_path, sender=sender,
            seen_path=_dd / "raw_channel_seen.txt",
        )
        return 0 if ok else 1
    except Exception as e:
        log.exception("resend failed: %s", e)
        return 1


def run_bilibili(no_push: bool = False) -> int:
    """Entry point for the hourly Bilibili digest (--bilibili-only). Polls
    whitelisted UPs via opencli, pushes new-video cards to the bilibili forum
    topic. Idempotent via the bvid SeenStore (marked seen only after a
    successful send); a failed/missed run is caught up by the next one thanks
    to the 48h lookback window."""
    tag = date.today().isoformat()
    configure_logging(log_file_for(f"bilibili-{tag}"))
    try:
        from chat_daily_tg.bilibili_digest import build_summarizer, push_digest
        from chat_daily_tg.bilibili_fetcher import (
            BridgeUnavailableError, fetch_new_videos, probe_bridge,
        )
        from chat_daily_tg.paths import BILIBILI_SEEN_PATH
        from chat_daily_tg.raw_seen import SeenStore
        from chat_daily_tg.tg_sender import TelegramSender

        load_env_file(DATA_DIR / ".env")
        cfg = load_config(CONFIG_PATH)
        src = cfg.sources.bilibili
        if not src.enabled or not src.fetch.whitelist:
            log.info("bilibili source disabled or whitelist empty, nothing to do")
            return 0
        if src.transport == "opencli":
            # Chrome-bridge preflight — the api transport has no local deps to probe.
            try:
                probe_bridge()
            except BridgeUnavailableError as e:
                # Common launchd cold-environment failure (Chrome/daemon not up) —
                # distinct message from a login expiry. The 48h lookback catches up
                # next run, so exit non-zero for visibility but nothing is lost.
                log.error("opencli bridge unavailable: %s", e)
                notify_failure("chat-daily-tg B站桥接不可用",
                               f"opencli daemon/Chrome bridge 不在线，本轮 digest 跳过（下轮自动追回）。{e}")
                return 1

        seen = SeenStore(BILIBILI_SEEN_PATH)
        videos = fetch_new_videos(
            src, seen,
            retry_max_attempts=cfg.retry.max_attempts,
            retry_backoff_seconds=cfg.retry.backoff_seconds,
        )
        log.info("bilibili new videos: %d", len(videos))
        if not videos:
            return 0

        sender = None
        if not no_push:
            dm_chat_id = os.environ[cfg.telegram.chat_id_env]
            chat_id, thread_id = resolve_tg_target(src.digest.topic, dm_chat_id)
            sender = TelegramSender(
                bot_token=os.environ[cfg.telegram.bot_token_env],
                chat_id=chat_id, message_thread_id=thread_id,
                retry_max_attempts=cfg.retry.max_attempts,
                retry_backoff_seconds=cfg.retry.backoff_seconds,
            )
            log.info("bilibili digest target: %s/thread=%s", chat_id, thread_id)
        workdir = prepare_archive_day(tag)
        sent = push_digest(videos, sender=sender, seen=seen, cfg=cfg,
                           summarizer=build_summarizer(cfg), workdir=workdir,
                           no_push=no_push)
        log.info("✓ bilibili digest complete: %d/%d cards sent (no_push=%s)",
                 sent, len(videos), no_push)
        return 0
    except Exception as e:
        log.exception("bilibili digest failed: %s", e)
        notify_failure("chat-daily-tg B站digest失败", f"{type(e).__name__}: {e}")
        return 1



def run_youtube(no_push: bool = False) -> int:
    """Entry point for the hourly YouTube digest (--youtube-only). Polls
    whitelisted channels via RSS (+ one Data API call for durations), pushes
    new-video cards to the youtube forum topic. Idempotent via the video_id
    SeenStore (marked seen only after a successful send); a failed/missed run
    is caught up by the next one thanks to the 48h lookback window.

    Cards are grouped by each channel's effective topic key (per-channel
    override → digest.topic default), one sender per topic — the hook for the
    planned non-tech clusters (英语学习 / 运动康复) to route elsewhere later."""
    tag = date.today().isoformat()
    configure_logging(log_file_for(f"youtube-{tag}"))
    try:
        from chat_daily_tg.paths import YOUTUBE_SEEN_PATH
        from chat_daily_tg.raw_seen import SeenStore
        from chat_daily_tg.tg_sender import TelegramSender
        from chat_daily_tg.youtube_digest import build_summarizer, push_digest
        from chat_daily_tg.youtube_fetcher import fetch_new_videos

        load_env_file(DATA_DIR / ".env")
        cfg = load_config(CONFIG_PATH)
        src = cfg.sources.youtube
        if not src.enabled or not src.fetch.whitelist:
            log.info("youtube source disabled or whitelist empty, nothing to do")
            return 0

        seen = SeenStore(YOUTUBE_SEEN_PATH)
        videos = fetch_new_videos(src, seen, api_key=os.environ.get(src.api_key_env))
        log.info("youtube new videos: %d", len(videos))
        if not videos:
            return 0

        by_topic: dict[str, list] = {}
        for v in videos:
            by_topic.setdefault(v.topic or src.digest.topic, []).append(v)

        workdir = prepare_archive_day(tag)
        summarizer = build_summarizer(cfg)
        sent = 0
        for topic_key, group in by_topic.items():
            sender = None
            if not no_push:
                dm_chat_id = os.environ[cfg.telegram.chat_id_env]
                chat_id, thread_id = resolve_tg_target(topic_key, dm_chat_id)
                sender = TelegramSender(
                    bot_token=os.environ[cfg.telegram.bot_token_env],
                    chat_id=chat_id, message_thread_id=thread_id,
                    retry_max_attempts=cfg.retry.max_attempts,
                    retry_backoff_seconds=cfg.retry.backoff_seconds,
                )
                log.info("youtube digest target: %s/thread=%s (topic=%s, %d cards)",
                         chat_id, thread_id, topic_key, len(group))
            sent += push_digest(group, sender=sender, seen=seen, cfg=cfg,
                                summarizer=summarizer, workdir=workdir,
                                no_push=no_push)
        log.info("✓ youtube digest complete: %d/%d cards sent (no_push=%s)",
                 sent, len(videos), no_push)
        return 0
    except Exception as e:
        log.exception("youtube digest failed: %s", e)
        # YouTube RSS multi-minute flake storms reopen due_gate every */5 and
        # would re-notify each tick; throttle the TG alert (log still records
        # every failure). Window matches run_youtube_r4s.sh ALERT_THROTTLE_S.
        if _alert_throttle_allow("youtube-digest", window_s=1200):
            notify_failure("chat-daily-tg YouTube digest失败", f"{type(e).__name__}: {e}")
        else:
            log.info("youtube digest failure alert throttled")
        return 1


def _llm_from_block(cfg, m):
    return LLMClient(
        endpoint=m.endpoint, model=m.model, api_key=os.environ[m.api_key_env],
        max_tokens=m.max_tokens, timeout=m.timeout,
        retry_max_attempts=cfg.retry.max_attempts,
        retry_backoff_seconds=cfg.retry.backoff_seconds,
        extra_body=m.extra_body,
    )


def _growth_llm(cfg):
    """Same construction main()'s _run uses for the summary model."""
    return _llm_from_block(cfg, cfg.models.summary)


def run_growth(no_push: bool = False, dm_test: bool = False,
               model_alias: str | None = None, mine_date: str | None = None) -> int:
    """Daily growth mining + one-card push (--growth-only), or mine-only for a
    given date (--growth-mine-day). Idempotent: day-level mined marker + daily
    send quota make the 09:30/15:30/21:30 catch-up schedule near-free reruns.
    --dm-test previews the winner card in the DM with ZERO state writes (no
    quota, no mark_sent, no ab log) so the real send still picks the same
    segment; --no-push prints both cards instead of sending, also stateless."""
    tag = date.today().isoformat()
    configure_logging(log_file_for(f"growth-{tag}"))
    try:
        from chat_daily_tg import growth_store
        from chat_daily_tg.growth_cards import build_card_a, build_card_b, judge
        from chat_daily_tg.growth_miner import GrowthMiningError, mine_day
        from chat_daily_tg.growth_weekly import poll_dm_feedback
        from chat_daily_tg.paths import (
            DB_PATH, GROWTH_FEEDBACK_INBOX, GROWTH_OFFSET_PATH, GROWTH_RUBRIC,
        )

        load_env_file(DATA_DIR / ".env")
        cfg = load_config(CONFIG_PATH)
        if model_alias:
            cfg.override_summary_model(model_alias)
            log.info("growth model overridden to: %s (%s)", model_alias, cfg.models.summary.model)
        g = cfg.growth
        if not g.enabled or g.source is None:
            log.info("growth mining disabled, nothing to do")
            return 0
        llm = _growth_llm(cfg)
        # 异源 judge：B 卡作者与评审分属两厂（deepseek 写、grok 评），消除同源
        # 自评偏好；别名/构造失败回落主模型，日卡永不因 judge 配置断供。
        judge_llm = llm
        if g.judge_model:
            try:
                judge_llm = _llm_from_block(cfg, cfg.resolve_model_alias(g.judge_model))
                log.info("growth judge model: %s (%s)", g.judge_model, judge_llm.model)
            except Exception as e:
                log.warning("growth judge alias %r unavailable (%s), judging with main model",
                            g.judge_model, e)
        today = date.today().isoformat()
        target_day = mine_date or yesterday_iso()

        try:
            inserted, found = mine_day(llm, cfg, target_day, sync=(mine_date is None))
            log.info("growth mine %s: %d candidates, %d queued", target_day, found, len(inserted))
        except GrowthMiningError as e:
            # Good chunks' segments are already queued; the day stays unmarked and
            # is re-mined by the next catch-up run. Don't block today's card.
            log.error("growth mining partial failure: %s", e)
            notify_failure("chat-daily-tg 成长挖掘部分失败", f"{target_day}: {e}")

        if mine_date is not None:
            return 0  # --growth-mine-day: mine only

        stateless = no_push or dm_test
        if not stateless and growth_store.sent_count_on(DB_PATH, today) >= g.daily_quota:
            log.info("growth daily quota (%d) reached, no push", g.daily_quota)
        else:
            seg = growth_store.pick_next(DB_PATH, prefer_date=target_day)
            if seg is None:
                log.info("growth queue empty, nothing to push today")
            else:
                rubric_text, rubric_version = growth_store.ensure_rubric(GROWTH_RUBRIC)
                card_a = build_card_a(seg)
                card_b = ""
                try:
                    card_b = build_card_b(llm, seg)
                    verdict = judge(judge_llm, card_a, card_b, rubric_text)
                except Exception as e:
                    # Style A is the zero-fabrication-risk deterministic card.
                    log.warning("card B/judge unavailable (%s), falling back to A", e)
                    verdict = {"winner": "A", "score_a": None, "score_b": None,
                               "reason": f"B/judge failed: {type(e).__name__}"}
                winner = verdict.get("winner", "A")
                winner_card = card_b if winner == "B" and card_b else card_a
                if no_push:
                    print(f"=== segment {seg.id} (score {seg.score}) ===")
                    print("--- Card A ---\n" + card_a)
                    print("--- Card B ---\n" + (card_b or "(generation failed)"))
                    print(f"--- verdict: {verdict}")
                else:
                    dm_chat_id = os.environ[cfg.telegram.chat_id_env]
                    if dm_test:
                        chat_id, thread_id = dm_chat_id, None
                    else:
                        chat_id, thread_id = resolve_tg_target(g.topic, dm_chat_id)
                        growth_store.log_ab(
                            DB_PATH, seg.id, rubric_version, winner,
                            verdict.get("score_a"), verdict.get("score_b"),
                            str(verdict.get("reason", ""))[:200], card_a, card_b)
                    sender = TelegramSender(
                        bot_token=os.environ[cfg.telegram.bot_token_env],
                        chat_id=chat_id, message_thread_id=thread_id,
                        retry_max_attempts=cfg.retry.max_attempts,
                        retry_backoff_seconds=cfg.retry.backoff_seconds,
                    )
                    # 电丸群消息一天一清，t.me 深链 24h 内必成死链——不放跳转按钮，
                    # 原文回查一律走本地切片（slice_path）。
                    sender.send_card(winner_card)
                    if not dm_test:  # write-after-send: crash retries the same segment
                        growth_store.mark_sent(DB_PATH, seg.id, style=winner)
                    log.info("✓ growth card %s style %s → %s/thread=%s (dm_test=%s)",
                             seg.id, winner, chat_id, thread_id, dm_test)

        # getUpdates only retains ~24h, so the DAILY job harvests DM feedback into
        # the durable inbox; the Saturday job consumes it. Failure never breaks the push.
        if not stateless:
            try:
                n = poll_dm_feedback(
                    os.environ[cfg.telegram.bot_token_env],
                    os.environ[cfg.telegram.chat_id_env],
                    offset_path=GROWTH_OFFSET_PATH, inbox_path=GROWTH_FEEDBACK_INBOX)
                if n:
                    log.info("growth feedback collected: %d", n)
            except Exception as e:
                log.warning("growth feedback poll failed: %s", e)
        return 0
    except Exception as e:
        log.exception("growth daily failed: %s", e)
        notify_failure("chat-daily-tg 成长挖掘失败", f"{type(e).__name__}: {e}")
        return 1


def run_growth_backfill(model_alias: str | None = None) -> int:
    """One-off history backfill (--growth-backfill): mine every day from
    cfg.growth.backfill_start through yesterday into the queue. Day-level
    resume via growth_mined_days; single-day failures are logged and skipped,
    summarized at the end. Sending stays with the daily quota job."""
    import time as _time

    tag = date.today().isoformat()
    configure_logging(log_file_for(f"growth-backfill-{tag}"))
    try:
        from chat_daily_tg import growth_store
        from chat_daily_tg.growth_miner import GrowthMiningError, _store_chat_id, mine_day
        from chat_daily_tg.paths import DB_PATH

        load_env_file(DATA_DIR / ".env")
        cfg = load_config(CONFIG_PATH)
        if model_alias:
            cfg.override_summary_model(model_alias)
        g = cfg.growth
        if not g.enabled or g.source is None:
            log.info("growth mining disabled, nothing to do")
            return 0
        llm = _growth_llm(cfg)
        chat_id = _store_chat_id(g.source.id)
        day = date.fromisoformat(g.backfill_start)
        # Stop at the day BEFORE yesterday: yesterday belongs exclusively to the
        # daily job, so a backfill running alongside it can never race the same
        # day's mined-marker + overlap-dedup window.
        end = date.today() - timedelta(days=2)
        total_inserted = total_found = 0
        failed: list[str] = []
        while day <= end:
            ds = day.isoformat()
            day += timedelta(days=1)
            if growth_store.day_already_mined(DB_PATH, chat_id, ds):
                continue
            try:
                inserted, found = mine_day(llm, cfg, ds, sync=False)
                total_inserted += len(inserted)
                total_found += found
                log.info("backfill %s: %d candidates, %d queued", ds, found, len(inserted))
            except GrowthMiningError as e:
                failed.append(ds)
                log.error("backfill %s failed: %s", ds, e)
            _time.sleep(1)  # gentle pacing between LLM days
        log.info("✓ growth backfill done: %d queued (%d candidates), %d days failed",
                 total_inserted, total_found, len(failed))
        if failed:
            notify_failure("chat-daily-tg 成长回填部分失败",
                           f"{len(failed)} 天失败（重跑自动续）: {', '.join(failed[:10])}")
            return 1
        return 0
    except Exception as e:
        log.exception("growth backfill failed: %s", e)
        notify_failure("chat-daily-tg 成长回填失败", f"{type(e).__name__}: {e}")
        return 1


def run_growth_weekly(no_push: bool = False, model_alias: str | None = None) -> int:
    """Saturday DM report (--growth-weekly): final feedback poll, fold feedback
    into the versioned rubric, then send the A/B + queue + content-drift report
    to the DM. Schedule is enforced by the launchd plist (Weekday=6), not here,
    so manual runs work any day."""
    tag = date.today().isoformat()
    configure_logging(log_file_for(f"growth-weekly-{tag}"))
    try:
        from chat_daily_tg.growth_miner import _store_chat_id
        from chat_daily_tg.growth_weekly import (
            build_weekly_report, consume_inbox, merge_rubric, poll_dm_feedback,
        )
        from chat_daily_tg.paths import (
            DB_PATH, GROWTH_FEEDBACK_INBOX, GROWTH_OFFSET_PATH, GROWTH_RUBRIC,
            GROWTH_RUBRIC_HISTORY,
        )

        load_env_file(DATA_DIR / ".env")
        cfg = load_config(CONFIG_PATH)
        if model_alias:
            cfg.override_summary_model(model_alias)
        g = cfg.growth
        if not g.enabled or g.source is None:
            log.info("growth mining disabled, nothing to do")
            return 0
        llm = _growth_llm(cfg)
        bot_token = os.environ[cfg.telegram.bot_token_env]
        dm_chat_id = os.environ[cfg.telegram.chat_id_env]

        try:  # catch the last <24h of feedback before consuming the inbox
            poll_dm_feedback(bot_token, dm_chat_id,
                             offset_path=GROWTH_OFFSET_PATH, inbox_path=GROWTH_FEEDBACK_INBOX)
        except Exception as e:
            log.warning("weekly feedback poll failed: %s", e)
        feedback = consume_inbox(GROWTH_FEEDBACK_INBOX)
        texts = [f["text"] for f in feedback]
        try:
            _, version, changed = merge_rubric(llm, GROWTH_RUBRIC, GROWTH_RUBRIC_HISTORY, texts)
        except Exception:
            # consume_inbox already rotated the inbox away; put the drained
            # entries back so next week's run retries the merge instead of
            # silently dropping this week's feedback.
            if feedback:
                with open(GROWTH_FEEDBACK_INBOX, "a", encoding="utf-8") as fh:
                    for entry in feedback:
                        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
            raise
        if changed:
            log.info("rubric updated to %s from %d feedback item(s)", version, len(texts))

        report = build_weekly_report(DB_PATH, _store_chat_id(g.source.id), llm, version, changed)
        if no_push:
            print(report)
        else:
            TelegramSender(
                bot_token=bot_token, chat_id=dm_chat_id,
                retry_max_attempts=cfg.retry.max_attempts,
                retry_backoff_seconds=cfg.retry.backoff_seconds,
            ).send(report, parse_mode="HTML")
        log.info("✓ growth weekly report done (rubric %s, changed=%s)", version, changed)
        return 0
    except Exception as e:
        log.exception("growth weekly failed: %s", e)
        notify_failure("chat-daily-tg 成长周报失败", f"{type(e).__name__}: {e}")
        return 1


_TIME_RE = re.compile(r"\d{2}:\d{2}")


def _citation_caption(analysis) -> str:
    """WeChat timestamps are 'YYYY-MM-DD HH:MM'; Telegram's are tz-suffixed ISO —
    regex-extract HH:MM instead of slicing so both formats caption correctly."""
    c = analysis.candidate
    m = _TIME_RE.search(c.timestamp)
    time_part = m.group(0) if m else c.timestamp
    return f"📷 {c.platform} · {c.group_name} · {time_part}"


def _push_rich_digest(
    tg,
    cfg,
    concise_md: str,
    citation_map: dict,
    *,
    health_rich_md: str = "",
    health_chart_path: Path | None = None,
) -> bool:
    """Send the digest as ONE rich message with the cited images INLINE at the
    LLM's [IMGn] marker positions. Bot API 10.2 media is uploaded directly in
    the multipart sendRichMessage call, avoiding the old public KV relay.
    Returns False on ANY failure so the caller falls back to text + photos."""
    try:
        # Rich messages consume MARKDOWN, so build from the RAW concise output
        # (post_process_concise would have converted [label](url) links into
        # Telegram-HTML <a> tags, which rich markdown must not receive).
        md = abbreviate_sources(concise_md, cfg.source_abbreviations)
        segments = resolve_citations(md, citation_map)
        if not health_rich_md and not health_chart_path and not any(
            analysis for _, analysis in segments
        ):
            return False
        parts: list[str] = [health_rich_md, "\n\n"] if health_rich_md else []
        media: list[tuple[str, str, str]] = []
        media_ids: dict[str, str] = {}
        if health_chart_path:
            media.append(("health_chart", str(health_chart_path), "photo"))
        for text_chunk, analysis in segments:
            parts.append(text_chunk)
            if analysis:
                path = analysis.candidate.local_path
                if path not in media_ids:
                    media_id = f"citation_{len(media_ids) + 1}"
                    media_ids[path] = media_id
                    media.append((media_id, path, "photo"))
                caption = _citation_caption(analysis).replace('"', "'")
                parts.append(
                    f'\n\n![](tg://photo?id={media_ids[path]} "{caption}")\n\n'
                )
        tg.send_rich_message(markdown="".join(parts), media=media)
        return True
    except Exception as e:
        log.warning("rich digest push failed, falling back to text+photo: %s", e)
        return False


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


def _run(date_str: str, *, model_alias: str | None = None, no_push: bool = False,
         wait_for_wake: bool = False, wake_deadline: str = "13:00") -> int:
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
    try:
        removed, freed = cleanup_old_media(cfg.archive.media_retention_days)
        if removed:
            log.info("media cleanup: removed %d dirs, freed %.1fMB", removed, freed / 1024 / 1024)
    except Exception as e:
        log.warning("media cleanup failed (non-fatal): %s", e)
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

    if wait_for_wake:
        # Single probe for this morning's sleep episode. If already synced, the
        # health card gets a real wake time; if missing, deliver the digest now
        # (sleep is optional — never spin until --wake-deadline).
        try:
            from chat_daily_tg.health_briefing import wait_for_wake_signal
            got = wait_for_wake_signal(
                cfg.health_briefing,
                wake_day=date.fromisoformat(date_str) + timedelta(days=1),
                timezone_name=cfg.schedule.timezone,
                deadline=wake_deadline,
            )
            if not got:
                log.info("proceeding without wake signal (no sleep data or disabled)")
        except Exception as e:
            log.warning("wake-signal wait failed (non-fatal, running now): %s", e)

    archive_dir = prepare_archive_day(date_str)
    # Channel cards are handled by the separate 2-hourly forwarder (run_channels), not
    # by this daily summary run.
    groups_with_content: list[tuple[str, str]] = []
    media_candidates = []
    citation_map = {}
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
            vision_stats: dict[str, int] = {}
            vision_audit: list[dict] = []
            analyses = analyze_media_candidates(
                client=vision_client, candidates=media_candidates,
                stats_out=vision_stats, audit_out=vision_audit,
                min_prefilter_score=cfg.models.vision.min_prefilter_score,
                min_include_score=cfg.models.vision.min_include_score,
                fallback_min_score=cfg.models.vision.fallback_min_score)
            failure = vision_zero_image_failure(vision_stats)
            if failure:
                # Distinguish "no high-value images today" (normal, roughly half
                # of days under the 0.8 bar) from a compromised pipeline — the
                # verdict predicate is shared with vision.py's ERROR log so the
                # TG alert and the log can never disagree (PR #8 review C4).
                notify_failure(
                    "chat-daily-tg 图片管线受损",
                    f"{date_str}: {failure}，日报可能无图（报告仍照常推送）。"
                    f"breakdown: {vision_stats}。日志: {log_file_for(date_str)}")
            write_vision_analyses(archive_dir / "vision.jsonl", analyses)
            try:
                # AFTER vision.jsonl: the audit trail is diagnostics — a failure
                # writing it must never discard the day's images (review A2).
                write_vision_audit(archive_dir / "vision-audit.jsonl", vision_audit)
            except Exception as e:
                log.warning("vision audit write failed (non-fatal): %s", e)
            vision_md = vision_markdown(analyses)
            (archive_dir / "vision.md").write_text(vision_md, encoding="utf-8")
            if vision_md.strip():
                groups_with_content.append(("图片理解 / 多来源", vision_md))
            citation_md, citation_map = build_citation_block(analyses)
            if citation_md:
                groups_with_content.append(("可引用图片列表", citation_md))
            log.info("vision analyses included: %d (citable: %d)", len(analyses), len(citation_map))
        except Exception as e:
            # A stage-level failure (missing api_key_env, client construction,
            # a raise before the per-image loop) produces the same imageless
            # digest as a low-value day — it must page, not just log, or the
            # 2026-07-14 silent-zero signature survives (PR #8 review C2).
            log.warning("vision analysis skipped: %s", e)
            notify_failure(
                "chat-daily-tg 图片阶段异常跳过",
                f"{date_str}: vision 阶段整体失败（{type(e).__name__}: {e}），"
                f"日报将无图（报告仍照常推送）。日志: {log_file_for(date_str)}")

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
    from chat_daily_tg.paths import DB_PATH

    perm_ctx = active_permanent_summary(DB_PATH)
    hot_ctx = active_hot_leads_summary(
        DB_PATH, retention_days=cfg.hot_leads.retention_days,
    )
    repeat_ctx = active_repeat_topics_summary(DB_PATH, today=date_str)

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
            embedder = GeminiEmbedder.from_config(embedding_model)
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

    health_plain = ""
    health_rich_md = ""
    health_chart_path: Path | None = None

    # Personal Health/Watch context is deterministic and remains outside the LLM
    # trust boundary. Failure is isolated so stale iCloud sync never blocks the
    # group-chat digest.
    if cfg.health_briefing.enabled:
        try:
            from chat_daily_tg.health_briefing import (
                build_health_report,
                format_health_briefing,
            )
            from chat_daily_tg.health_card import render_health_card
            from chat_daily_tg.health_rich import build_health_rich_markdown

            health_report = build_health_report(
                date.fromisoformat(date_str), cfg.health_briefing, cfg.schedule.timezone,
            )
            if health_report:
                health_plain = format_health_briefing(health_report)
                (archive_dir / "health-briefing.md").write_text(
                    health_plain, encoding="utf-8"
                )
                health_chart_path = render_health_card(
                    health_report, archive_dir / "health-card.png"
                )
                health_rich_md = build_health_rich_markdown(
                    health_report,
                    chart_media_id="health_chart" if health_chart_path else None,
                )
                (archive_dir / "health-rich.md").write_text(
                    health_rich_md, encoding="utf-8"
                )
                out = replace(out, concise_md=f"{health_plain}\n\n{out.concise_md}")
        except Exception as e:
            log.warning("health briefing skipped (non-fatal): %s", e)

    # Dedup audit footer: silence must be auditable — the user should learn
    # "N cards were withheld today" from the report itself, not from grepping
    # two machines' logs. Empty counts add nothing (footer omitted).
    try:
        from chat_daily_tg.dedup_journal import today_counts
        counts = today_counts()
        if counts:
            line = "♻️ 今日频道去重：" + "、".join(
                f"{layer} {n} 条" for layer, n in sorted(counts.items()))
            out = replace(out, concise_md=f"{out.concise_md}\n\n{line}")
    except Exception as e:
        log.warning("dedup footer skipped (non-fatal): %s", e)

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
    if not citation_map:
        # No citable images today → resolve_citations never runs, so any [IMGn]
        # the LLM emitted anyway must be stripped here or it reaches the reader
        # as a literal bracket token.
        from chat_daily_tg.vision import strip_citation_markers
        concise_processed = strip_citation_markers(concise_processed)

    # Guard against empty or near-empty output after repair
    if len(concise_processed.strip()) < 100:
        log.error("concise output too short (%d chars), skipping TG push", len(concise_processed.strip()))
        notify_failure("chat-daily-tg 日报生成异常", f"精简版输出过短（{len(concise_processed.strip())} 字符），可能 LLM 格式解析失败。日志: {log_file_for(date_str)}")
        return 1

    # 4.5. Persist opportunities. Persistence is idempotent across same-day catch-up
    # reruns (the .persisted marker guards re-appending non-deterministic hot-lead ids,
    # review #40) and fully isolated: any failure here — persistence OR derived-view
    # regeneration — logs but NEVER blocks the already-generated report push (review #43).
    from datetime import datetime as _dt
    from chat_daily_tg.paths import (
        DB_PATH, PERMANENT_MD, HOT_LEADS_DIR, HOT_LEADS_LATEST,
    )
    persisted_marker = archive_dir / PERSISTED_MARKER
    try:
        if persisted_marker.exists():
            log.info("opportunities already persisted for %s, skipping re-persist (catch-up)", date_str)
        else:
            _persist_opportunities(out, date_str, HOT_LEADS_DIR)
            persisted_marker.write_text(_dt.now().isoformat(), encoding="utf-8")
        # Derived views regenerate from the DB each run (idempotent).
        from chat_daily_tg.permanent_md import regenerate_permanent_md
        from chat_daily_tg.hot_leads import regenerate_latest
        regenerate_permanent_md(DB_PATH, PERMANENT_MD)
        regenerate_latest(DB_PATH, HOT_LEADS_LATEST, retention_days=cfg.hot_leads.retention_days)
    except Exception as e:
        log.warning("opportunity persistence/regeneration failed (non-fatal, report still pushes): %s", e)
        # The report still ships, but the day's opportunities didn't persist — and
        # a successful push writes COMPLETE, so catch-up won't retry. Surface it
        # rather than lose the data silently.
        notify_failure("chat-daily-tg 机会持久化失败",
                       f"{type(e).__name__}: {e}（报告已照常推送，当天机会可能未入库）")

    if not no_push:
        bot_token = os.environ[cfg.telegram.bot_token_env]
        dm_chat_id = os.environ[cfg.telegram.chat_id_env]
        chat_id, thread_id = resolve_tg_target("chat_daily", dm_chat_id)
        tg = TelegramSender(
            bot_token=bot_token, chat_id=chat_id, message_thread_id=thread_id,
            retry_max_attempts=cfg.retry.max_attempts,
            retry_backoff_seconds=cfg.retry.backoff_seconds,
        )
        # Per-stage sent markers: COMPLETE_MARKER is only written after the WHOLE
        # push, so a failure between a delivered stage and that marker made the
        # same-day catch-up re-send the already-delivered card/digest. The rich
        # and card sends have no chunk-level resume (unlike .text-push-state.json),
        # so day-level "this stage already delivered" is the right idempotency
        # granularity — a catch-up rerun regenerates DIFFERENT text, and the day's
        # digest must still go out at most once.
        card_marker = archive_dir / ".card-sent"
        digest_marker = archive_dir / ".digest-sent"
        health_card_marker = archive_dir / ".health-card-sent"
        image_sent = card_marker.exists()
        if image_sent:
            log.info("card already sent for %s, skipping (catch-up)", date_str)
        elif cfg.telegram.send_image and digest_marker.exists():
            # A prior run delivered the digest but its card failed — a catch-up
            # card would now arrive AFTER the text, inverting the card-first
            # contract. The card is a glanceable add-on; drop the late one.
            log.info("digest already sent for %s, skipping late card (catch-up)", date_str)
        elif cfg.telegram.send_image:
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
                    card_marker.write_text(_dt.now().isoformat(), encoding="utf-8")
                    log.info("TG card image sent")
            except Exception as e:
                log.warning("card image push failed, falling back to text: %s", e)
        if digest_marker.exists():
            log.info("digest already sent for %s, skipping (catch-up)", date_str)
        elif image_sent and cfg.telegram.send_image and cfg.telegram.image_only:
            # send_image gates the skip too: if the operator turned the card off
            # after a day's card already went out, they want the text after all —
            # a stale .card-sent must not turn the day into a silent no-op.
            # Image-only mode: the card was delivered, so skip the full text message.
            # (Text still sends below if the image failed — image_sent would be False.)
            log.info("image_only mode: skipping text push")
        else:
            # The LLM's [IMGn] markers only PICK the image (at most one, AI-preferred);
            # the digest text itself always goes out as one intact message (markers
            # stripped), so the multi-chunk resume via state_path stays safe (review
            # finding #42). The chosen photo follows as its own trailing message —
            # Telegram has no single-message text+image format that fits a full
            # digest (caption cap is 1024 visible chars; user decision 2026-07-02).
            segments = resolve_citations(concise_processed, citation_map) if citation_map else []
            cited_list = [analysis for _, analysis in segments if analysis]
            if citation_map and not cited_list:
                # Images were included/promoted but the summary LLM emitted no
                # [IMGn] marker — the digest ships imageless while stats and
                # vision.jsonl claim otherwise. Leave the true attribution in
                # the log or this misattribution recurs (PR #8 review, sweep).
                log.warning("vision included %d citable image(s) but the digest "
                            "cites none — LLM declined to cite", len(citation_map))
            rich_digest_md = out.concise_md
            if health_plain and rich_digest_md.startswith(f"{health_plain}\n\n"):
                rich_digest_md = rich_digest_md[len(health_plain) + 2:]
            chart_for_rich = (
                health_chart_path
                if health_chart_path and not health_card_marker.exists()
                else None
            )
            rich_ok = _push_rich_digest(
                tg,
                cfg,
                rich_digest_md,
                citation_map,
                health_rich_md=health_rich_md,
                health_chart_path=chart_for_rich,
            )
            if rich_ok:
                digest_marker.write_text(_dt.now().isoformat(), encoding="utf-8")
                if chart_for_rich:
                    health_card_marker.write_text(
                        _dt.now().isoformat(), encoding="utf-8"
                    )
                log.info(
                    "TG push complete (single rich message, health_chart=%s, %d cited image(s))",
                    bool(chart_for_rich), len(cited_list),
                )
            else:
                if health_chart_path and not health_card_marker.exists():
                    try:
                        tg.send_photo(
                            health_chart_path,
                            caption=f"📊 昨日健康概览 · {date_str} · {health_report.sleep_label}",
                        )
                        health_card_marker.write_text(
                            _dt.now().isoformat(), encoding="utf-8"
                        )
                    except Exception as e:
                        log.warning(
                            "health card fallback send failed; continuing text-only: %s",
                            e,
                        )
                full_text = "".join(chunk for chunk, _ in segments) if segments else concise_processed
                tg.send(full_text, parse_mode="HTML",
                        state_path=archive_dir / ".text-push-state.json")
                digest_marker.write_text(_dt.now().isoformat(), encoding="utf-8")
                sent = 0
                for cited in cited_list:
                    try:
                        tg.send_media(cited.candidate.local_path, "photo",
                                      caption=_citation_caption(cited))
                        sent += 1
                    except Exception as e:
                        log.warning("citation image send failed (%s): %s",
                                    cited.candidate.raw_ref, e)
                log.info("TG push complete (%d/%d trailing cited image(s))", sent, len(cited_list))
    else:
        log.info("TG push skipped (--no-push)")

    if not no_push:
        # Only a pushed run counts as delivered — a --no-push debug run must not
        # suppress the same-day catch-up retries.
        (archive_dir / COMPLETE_MARKER).write_text(_dt.now().isoformat(), encoding="utf-8")
    log.info("✓ run_daily complete for %s", date_str)
    return 0


_TERM_ALERTED = False


def _alert_throttle_allow(key: str, *, window_s: int = 1200) -> bool:
    """Return True if an alert for ``key`` may fire now; records the stamp.

    Used to absorb YouTube RSS multi-tick storms that would otherwise re-notify
    every due_gate reopen (*/5). Fail-open: any I/O error allows the alert.
    """
    import time as _time
    stamp = DATA_DIR / "state" / f"alert-throttle-{key}"
    try:
        stamp.parent.mkdir(parents=True, exist_ok=True)
        now = _time.time()
        if stamp.is_file():
            try:
                last = float(stamp.read_text(encoding="utf-8").strip() or "0")
            except ValueError:
                last = 0.0
            if last > 0 and (now - last) < window_s:
                return False
        stamp.write_text(f"{now:.0f}\n", encoding="utf-8")
        return True
    except OSError:
        return True


def _install_termination_alert() -> None:
    """Turn a mid-run SIGTERM/SIGHUP into an alert instead of a silent death.

    launchd `unload`/`bootout` (schedule.py retiming the agents, a reinstall, logout,
    or shutdown) SIGTERMs the whole job — bash wrapper included — so the guard's
    exit-code alert path never runs and the day's report vanishes with zero signal:
    no heartbeat, no marker, no retry (single 07:05 trigger). 2026-07-18 a concurrent
    `schedule.py apply` killed the 07:05 run mid-vision, before push, unnoticed until
    asked. Catch the terminating signals here — inside Python, above bash — fire one
    best-effort alert, then re-raise the signal's default action so the exit status
    still reflects it (128+signum) and launchd reaps us without escalating to SIGKILL.

    notify_failure does the offline macOS notification first (fast) and the TG send
    second (best-effort, 15s cap), so even if launchd's ~20s ExitTimeOut SIGKILLs us
    mid-send the local notification has already fired.
    """
    def _handler(signum: int, _frame) -> None:
        global _TERM_ALERTED
        if not _TERM_ALERTED:  # a second signal mid-handler must not re-enter
            _TERM_ALERTED = True
            name = signal.Signals(signum).name
            try:
                notify_failure(
                    "chat-daily-tg 被中断",
                    f"进程收到 {name}，在交付前被外部终止"
                    "（launchd unload/reinstall、登出或关机）。当天日报可能未送达——"
                    "检查 .run-complete / 日志，缺失则补跑 `run_daily.py --date <当天>`。",
                )
            except Exception:  # noqa: BLE001 — a terminal path must never mask the exit
                pass
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)

    for _sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(_sig, _handler)


if __name__ == "__main__":
    # launchd inherits Shadowrocket's ALL_PROXY=socks5://… (launchctl setenv);
    # scrub before any httpx client is built or the whole pipeline dies at
    # Client() construction (2026-07-03 outage, all three jobs).
    scrub_socks_proxy_env()
    # Alert-on-termination: a launchd unload/reinstall (or logout/shutdown) SIGTERMs
    # the job past the guard's exit-code alert, so make Python itself surface it.
    _install_termination_alert()
    p = argparse.ArgumentParser()
    p.add_argument("--date", help="YYYY-MM-DD (default: yesterday)", default=None)
    p.add_argument("--model", help="Model alias from config (e.g. 'gemini')", default=None)
    p.add_argument("--no-push", action="store_true", help="Skip Telegram push")
    p.add_argument("--skip-if-done", action="store_true",
                   help="Exit 0 immediately if this date's run already completed "
                        "(re-fired/coalesced launchd trigger, manual re-run)")
    p.add_argument("--wait-for-wake", action="store_true",
                   help="Probe once for this morning's Watch sleep episode; if already "
                        "synced use it for the health card, otherwise deliver immediately")
    p.add_argument("--wake-deadline", metavar="HH:MM", default="13:00",
                   help="Deprecated (no-wait policy); kept for launchd EnvironmentVariables "
                        "compat (default 13:00)")
    p.add_argument("--channels-only", action="store_true",
                   help="Run only the 2-hourly verbatim channel forwarder (no summary)")
    p.add_argument("--bilibili-only", action="store_true",
                   help="Run only the hourly Bilibili subscription digest (no summary)")
    p.add_argument("--youtube-only", action="store_true",
                   help="Run only the hourly YouTube subscription digest (no summary)")
    p.add_argument("--growth-only", action="store_true",
                   help="Run only the daily growth mining + card push")
    p.add_argument("--growth-mine-day", metavar="YYYY-MM-DD", default=None,
                   help="Mine one specific day into the growth queue, no push (debug)")
    p.add_argument("--growth-backfill", action="store_true",
                   help="Backfill growth mining from cfg.growth.backfill_start (queue only)")
    p.add_argument("--growth-weekly", action="store_true",
                   help="Send the weekly growth A/B report to the DM")
    p.add_argument("--dm-test", action="store_true",
                   help="With --growth-only: send the winner card to the DM, zero state writes")
    p.add_argument("--resend", metavar="CHAT_ID:MSG_ID", default=None,
                   help="Rebuild and send ONE channel card, bypassing seen/HWM and every "
                        "dedup layer — the recovery hatch for a wrong suppression "
                        "(find the ids in the dedup journal / rawcard archive)")
    args = p.parse_args()
    if args.resend:
        sys.exit(run_resend(args.resend))
    if args.channels_only:
        sys.exit(run_channels(no_push=args.no_push))
    if args.bilibili_only:
        sys.exit(run_bilibili(no_push=args.no_push))
    if args.youtube_only:
        sys.exit(run_youtube(no_push=args.no_push))
    if args.growth_only or args.growth_mine_day:
        sys.exit(run_growth(no_push=args.no_push, dm_test=args.dm_test,
                            model_alias=args.model, mine_date=args.growth_mine_day))
    if args.growth_backfill:
        sys.exit(run_growth_backfill(model_alias=args.model))
    if args.growth_weekly:
        sys.exit(run_growth_weekly(no_push=args.no_push, model_alias=args.model))
    sys.exit(main(date_str=args.date, model_alias=args.model, no_push=args.no_push,
                  skip_if_done=args.skip_if_done, wait_for_wake=args.wait_for_wake,
                  wake_deadline=args.wake_deadline))
