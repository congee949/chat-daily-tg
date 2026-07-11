"""Weekly growth-mining job: drain DM feedback, fold it into the judge rubric,
and build the Saturday HTML report.

The bot is send-only, and Telegram's getUpdates only retains updates ~24h, so
feedback DMs are polled DAILY (the daily growth job calls poll_dm_feedback at
its tail) into a durable JSONL inbox. This module's weekly job then consumes
that inbox, merges the feedback into a versioned rubric, and assembles the
report; run_daily.py (another lane) is responsible for actually sending it.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
import json
import logging

import httpx

from chat_daily_tg.growth_store import (
    LOCAL_TZ,
    ab_stats,
    ensure_rubric,
    latest_ab_pair,
    mined_days_summary,
    queue_stats,
    recent_sent,
    rubric_version_of,
)
from chat_daily_tg.tg_sender import escape_html

log = logging.getLogger(__name__)


# ------------------------------------------------------------- DM feedback inbox

def _read_offset(offset_path: Path) -> int:
    try:
        return int(Path(offset_path).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0


def _write_offset(offset_path: Path, offset: int) -> None:
    offset_path = Path(offset_path)
    offset_path.parent.mkdir(parents=True, exist_ok=True)
    offset_path.write_text(str(offset), encoding="utf-8")


def poll_dm_feedback(bot_token: str, dm_chat_id: str, *,
                      offset_path: Path, inbox_path: Path) -> int:
    """Drain getUpdates into the durable feedback inbox.

    Every update (including ones outside the DM chat, and ones with no text)
    advances the offset high-water mark so it is never re-delivered; only DM
    text messages are appended to inbox_path. The offset is persisted after
    each processed batch so a crash mid-drain loses at most one in-flight
    batch's worth of progress (a duplicated inbox line, deduped at consume
    time) rather than the whole run.
    """
    offset_path = Path(offset_path)
    inbox_path = Path(inbox_path)
    high_water = _read_offset(offset_path)
    url = f"https://api.telegram.org/bot{bot_token}/getUpdates"
    total = 0
    with httpx.Client(timeout=30.0) as client:
        while True:
            params = {
                "offset": high_water + 1,
                "timeout": 0,
                "allowed_updates": json.dumps(["message"]),
            }
            r = client.get(url, params=params)
            if r.status_code == 409:
                log.error("getUpdates 409 conflict (another consumer/webhook holds this bot token)")
                return 0
            r.raise_for_status()
            updates = r.json().get("result", [])
            if not updates:
                break
            batch_floor = high_water
            lines: list[str] = []
            for update in updates:
                uid = update.get("update_id")
                if uid is not None:
                    high_water = max(high_water, uid)
                message = update.get("message")
                if not message or "text" not in message:
                    continue
                chat = message.get("chat") or {}
                if str(chat.get("id")) != str(dm_chat_id):
                    continue
                lines.append(json.dumps(
                    {"update_id": uid, "date": message.get("date"), "text": message["text"]},
                    ensure_ascii=False))
            if lines:
                inbox_path.parent.mkdir(parents=True, exist_ok=True)
                with inbox_path.open("a", encoding="utf-8") as fh:
                    for line in lines:
                        fh.write(line + "\n")
                total += len(lines)
            # Persisted after every batch (not just once at the end) so offset
            # progress survives a crash on the NEXT iteration's request.
            _write_offset(offset_path, high_water)
            if high_water == batch_floor:
                # A non-empty batch that advanced nothing (malformed update_ids)
                # would loop forever re-fetching the same page — bail out.
                log.error("getUpdates batch advanced no offset, aborting poll")
                break
    return total


def _processed_path(inbox_path: Path) -> Path:
    today = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")
    candidate = inbox_path.parent / f"feedback-processed-{today}.jsonl"
    if not candidate.exists():
        return candidate
    i = 2
    while True:
        candidate = inbox_path.parent / f"feedback-processed-{today}-{i}.jsonl"
        if not candidate.exists():
            return candidate
        i += 1


def consume_inbox(inbox_path: Path) -> list[dict]:
    """Drain the feedback inbox for the weekly merge: dedup by update_id, sort
    by it, then rotate the file out of the way so it can't be double-consumed
    by a re-run later. Missing or empty file (nothing to process) → []."""
    inbox_path = Path(inbox_path)
    if not inbox_path.exists():
        return []
    raw = inbox_path.read_text(encoding="utf-8")
    if not raw.strip():
        return []
    entries: dict[int, dict] = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            log.warning("consume_inbox: skipping unparseable line: %r", line[:200])
            continue
        entries[entry.get("update_id")] = entry
    result = [entries[k] for k in sorted(entries.keys())]
    inbox_path.rename(_processed_path(inbox_path))
    return result


# ------------------------------------------------------------------------ rubric

_RUBRIC_SYSTEM = "你是「成长卡片」评审偏好 rubric 的维护者，只根据用户反馈调整评审标准本身，不生成卡片内容。"


def _build_rubric_prompt(current_text: str, feedback_texts: list[str], expected_header: str) -> str:
    numbered = "\n".join(f"{i}. {t}" for i, t in enumerate(feedback_texts, start=1))
    return f"""当前评审偏好 rubric（Markdown 全文）：

{current_text}

用户在成长卡片私聊里发来的反馈，按时间顺序编号：

{numbered}

请只吸收「内容/风格偏好」类反馈（例如嫌啰嗦、要求金句更严格、语气要更克制等），
和评审规则无关的闲聊、寒暄一律忽略。
输出完整的新版 rubric（Markdown 全文，不是增量 diff）。
首行必须原样是：
{expected_header}
"""


def _enforce_rubric_header(text: str, expected_header: str) -> str:
    """The LLM is asked to open with `expected_header` but can't be trusted to
    comply — a missing/garbled first line would silently break rubric_version_of
    downstream. Deterministically force it: replace a wrong heading line, or
    prepend one if the model skipped the header entirely."""
    lines = text.split("\n")
    first = lines[0].strip() if lines else ""
    if first == expected_header:
        return text
    if first.startswith("#"):
        lines[0] = expected_header
    else:
        lines.insert(0, expected_header)
    return "\n".join(lines)


def merge_rubric(llm, rubric_path: Path, history_dir: Path,
                  feedback_texts: list[str]) -> tuple[str, str, bool]:
    """Fold this week's DM feedback into the judge rubric via one LLM call."""
    rubric_path = Path(rubric_path)
    history_dir = Path(history_dir)
    current_text, current_version = ensure_rubric(rubric_path)
    if not feedback_texts:
        return current_text, current_version, False

    today = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")
    digits = current_version[1:]
    current_n = int(digits) if digits.isdigit() else 0
    new_version = f"v{current_n + 1}"
    expected_header = f"# 成长卡片评审偏好 {new_version}（{today}）"

    content, _usage = llm.chat(
        _build_rubric_prompt(current_text, feedback_texts, expected_header),
        system=_RUBRIC_SYSTEM)

    if not content or len(content.strip()) < 40:
        # 空的或短到不可能是完整 rubric 的输出，直接放弃本次合并，保住现版本。
        log.warning("merge_rubric: LLM output empty/too short, keeping rubric %s", current_version)
        return current_text, current_version, False

    new_text = _enforce_rubric_header(content.strip("\n"), expected_header)
    resolved_version = rubric_version_of(new_text)

    history_dir.mkdir(parents=True, exist_ok=True)
    (history_dir / f"rubric-{current_version}-{today}.md").write_text(current_text, encoding="utf-8")
    rubric_path.write_text(new_text if new_text.endswith("\n") else new_text + "\n", encoding="utf-8")

    return new_text, resolved_version, True


# ------------------------------------------------------------------ weekly report

_ANALYSIS_SYSTEM = "你是「成长卡片」内容编辑，只做质量诊断，不生成新卡片、不改写原文。"
_SAMPLE_TRUNCATE = 400


def _truncate(text: str, limit: int) -> str:
    return text[:limit]


def _fmt_score(score: float | None) -> str:
    return f"{score:.1f}" if score is not None else "?"


def _summarize_recent(llm, segments: list) -> str:
    listing = "\n".join(
        f"- [{seg.date}] {seg.theme}：{'；'.join(seg.points)}" for seg in segments)
    prompt = f"""过去 28 天已发送的成长卡片主题与要点：

{listing}

请用 3-5 句话评估这批卡片：主题/要点是否重复、是否单调、风格是否漂移。
只输出这 3-5 句评价本身，不要列表、不要标题、不要客套话。
"""
    content, _usage = llm.chat(prompt, system=_ANALYSIS_SYSTEM)
    return content.strip()


def build_weekly_report(store_db: Path, chat_id: int, llm,
                         rubric_version: str, rubric_changed: bool) -> str:
    """Assemble the Saturday DM report from growth_store data. Returns Telegram
    HTML; the caller sends it (e.g. TelegramSender.send(..., parse_mode="HTML"))."""
    store_db = Path(store_db)
    q = queue_stats(store_db)
    ab = ab_stats(store_db, recent_days=7)
    mined = mined_days_summary(store_db, chat_id)
    sent = recent_sent(store_db, days=28)
    pair = latest_ab_pair(store_db)

    lines = ["<b>🌱 成长挖掘周报</b>", ""]

    lines.append("<b>数据</b>")
    lines.append(f"待发 {q['pending']} · 已发 {q['sent']} · 已拒 {q['rejected']}")
    lines.append(f"A/B 胜出（7天）：A {ab['recent']['A']} : B {ab['recent']['B']}")
    lines.append(f"A/B 胜出（累计）：A {ab['total']['A']} : B {ab['total']['B']}")
    if mined["days"]:
        lines.append(
            f"回填进度：{mined['days']} 天（{escape_html(str(mined['first']))} – "
            f"{escape_html(str(mined['last']))}）")
    else:
        lines.append("回填进度：暂无挖掘记录")
    lines.append("")

    lines.append("<b>内容分析</b>")
    if not sent:
        lines.append("本周暂无已发送卡片")
    else:
        lines.append(escape_html(_summarize_recent(llm, sent)))
    lines.append("")

    lines.append("<b>样例对照</b>")
    if pair is None:
        lines.append("暂无 A/B 评审样例")
    else:
        lines.append(f"A：{escape_html(_truncate(pair['card_a'], _SAMPLE_TRUNCATE))}")
        lines.append(f"B：{escape_html(_truncate(pair['card_b'], _SAMPLE_TRUNCATE))}")
        lines.append(
            f"胜出：{escape_html(pair['winner'])}"
            f"（A {_fmt_score(pair['score_a'])} : B {_fmt_score(pair['score_b'])}）")
        if pair["verdict"]:
            lines.append(escape_html(pair["verdict"]))
    lines.append("")

    rubric_line = f"评审版本：{escape_html(rubric_version)}"
    if rubric_changed:
        rubric_line += "（本周已按你的反馈更新）"
    lines.append(rubric_line)

    return "\n".join(lines)
