from __future__ import annotations

from collections import Counter
from datetime import date, timedelta
from pathlib import Path
import logging
import math
import re

from chat_daily_tg import growth_store, paths
from chat_daily_tg.growth_store import GrowthSegment
from chat_daily_tg.summarizer import _safe_json_loads
from chat_daily_tg.telegram_exporter import (
    LOCAL_TZ,
    parse_timestamp,
    read_messages,
    should_skip_content,
    sync_chat,
)

log = logging.getLogger(__name__)

# LLM 输出是不可信输入：一个对话段落被采纳前，它的 msg-id 边界、跨度长度、
# 时长、金句都要逐条对 messages.db 复核（见 _validate_segment）。校验通不过的
# 段落静默丢弃，不影响同一天其余 chunk 的处理。
MAX_SEGMENT_HOURS = 4
GAP_SPLIT_MINUTES = 10
HARD_SPLIT_FACTOR = 1.5
MAX_QUOTES = 3
# 金句去空白后的长度窗口——"是"/"好的" 这类碎片过校验只会稀释金句通道，
# 也放大断章取义的可利用面；超长"金句"不是金句，还会把卡片顶过 Telegram
# 3900 字分块线，带来多 chunk 部分重发的窗口（对抗评审 2026-07-11）。
MIN_QUOTE_CHARS = 6
MAX_QUOTE_CHARS = 160

MINER_SYSTEM = """你是群聊「个人成长」内容挖掘助手。从下面的群聊转写中，挑出对个人长期成长\
有价值的完整对话段落。

【关注】思维方式、职业与金钱价值观、心态开导、方法论、认知纠偏。
【排除】日常闲聊、八卦、行情喊单、纯技术互助、无观点的信息转发。

硬性要求：
1. 每个段落必须是一段完整的对话弧（起因 → 展开 → 收束），不要截取半句或掐头去尾。
2. start_msg_id / end_msg_id 只能取转写里真实出现过的 [msg_id]，不得虚构、推算或改写。
3. quotes 必须逐字复制原文，保留原有标点与空格，禁止改写、润色或拼接；每段最多 3 条，
   msg_id 指向被引用那条消息。
4. score 为 0-10 的数字，衡量观点密度、对普通人的可迁移性、以及对话完整性。
5. theme、points 用简洁中文概括；participants 可省略（由系统按发言量自行计算）。
   每条 point 里可以用 **…** 标出最关键的短语（0-2 处），除此之外不要用任何 Markdown 语法。
6. 严格只输出 JSON，不要加解释文字或 ``` 代码块围栏。没有合格段落时输出 {"segments": []}。

输出 JSON schema 示例：
{
  "segments": [
    {
      "start_msg_id": 1782500,
      "end_msg_id": 1782540,
      "theme": "为什么省钱不如创造价值",
      "points": ["把注意力放在**开源**而非节流", "价值来自**创造**而非节省"],
      "quotes": [{"msg_id": 1782520, "sender": "A K", "text": "价值是创造出来的 不是节省出来的"}],
      "participants": ["A K"],
      "score": 8,
      "reason": "观点密度高、可迁移性强"
    }
  ]
}
"""


class GrowthMiningError(Exception):
    """Raised after inserting the good chunks when >=1 chunk failed to parse —
    the day is deliberately NOT marked mined so a retry re-processes it."""


# --------------------------------------------------------------------- helpers

_WS_RE = re.compile(r"\s+")
_INT_RE = re.compile(r"-?\d+")
# MINER_SYSTEM forbids code fences, but LLMs wrap JSON in ```json … ``` anyway;
# peel one wrapper before parsing so a compliant-content-but-fenced reply isn't
# counted as a failed chunk. True garbage still has no fence and still fails.
_FENCE_RE = re.compile(r"^\s*```[^\n]*\n(.*)\n```\s*$", re.DOTALL)


def _parse_llm_json(content: str, label: str) -> dict:
    """Strip an outer markdown code fence if present, then reuse the summarizer's
    tolerant JSON loader (its sanitizer fixes trailing commas / truncation)."""
    fenced = _FENCE_RE.match(content)
    if fenced:
        content = fenced.group(1)
    return _safe_json_loads(content, label)


def _strip_ws(text: str) -> str:
    """Remove every whitespace char so a quote's verbatim check ignores the LLM's
    line-wrapping / spacing (only whitespace is allowed to differ)."""
    return _WS_RE.sub("", text or "")


def _as_int(value) -> int | None:
    """Coerce an LLM-supplied id to int, tolerating stringified / float ids;
    returns None for anything non-integral so the caller can drop it."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    if isinstance(value, str) and _INT_RE.fullmatch(value.strip()):
        return int(value.strip())
    return None


def _coerce_score(value) -> float:
    """0-10 float; anything non-numeric, non-finite, negative or >10 → 0.0.
    Never clamp upward — 0.0 guarantees rejection by min_score."""
    try:
        score = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(score) or score < 0 or score > 10:
        return 0.0
    return score


def _store_chat_id(source_id: str | int) -> int:
    """Positive DB chat_id (e.g. 1162433032) from the config form
    (-1001162433032) — same -100 stripping as telegram_exporter.canonical_chat_ids."""
    digits = str(abs(int(source_id)))
    if digits.startswith("100") and len(digits) > 3:
        digits = digits[3:]
    return int(digits)


def _transcript_line(row) -> str:
    hm = parse_timestamp(row["timestamp"]).astimezone(LOCAL_TZ).strftime("%H:%M")
    sender = row["sender_name"] or "unknown"
    content = row["content"] or ""
    return f"[{row['msg_id']}] {hm} {sender}: {content}"


def _build_prompt(chat_name: str, date_str: str, transcript: str) -> str:
    return (
        f"以下是群「{chat_name}」{date_str}（北京时间）的聊天转写，"
        f"每行格式为 [msg_id] HH:MM 发言人: 内容。\n"
        f"请按系统提示挑出有长期个人成长价值的完整对话段落，严格只输出 JSON。\n\n"
        f"{transcript}"
    )


def _clean_points(raw) -> list[str]:
    """长度按去掉 **…** 加粗标记后的纯文本算：超限的点丢标记存纯文本截断，
    否则原样保留（标记由卡片渲染层转 <b>）。"""
    if not isinstance(raw, list):
        return []
    points: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        item = item.strip()
        if not item:
            continue
        plain = item.replace("**", "")
        if not plain:
            continue
        points.append(item if len(plain) <= 80 else plain[:80])
        if len(points) >= 5:
            break
    return points


def _participants(span_rows: list) -> str:
    """Ignore the LLM's list — compute from the span: unique senders ordered by
    descending message count, capped at 6, comma-joined."""
    counts = Counter((row["sender_name"] or "unknown") for row in span_rows)
    return ", ".join(name for name, _ in counts.most_common(6))


def _verbatim_slice(content: str, needle: str) -> str | None:
    """Exact substring of `content` whose whitespace-stripped form equals
    `needle`. Cards display THIS, not the LLM's copy — so what the reader sees
    is byte-for-byte the DB original (spacing included), never LLM re-spacing."""
    stripped_chars: list[str] = []
    orig_idx: list[int] = []
    for i, ch in enumerate(content):
        if not _WS_RE.match(ch):
            stripped_chars.append(ch)
            orig_idx.append(i)
    pos = "".join(stripped_chars).find(needle)
    if pos < 0:
        return None
    return content[orig_idx[pos]:orig_idx[pos + len(needle) - 1] + 1]


def _validate_quotes(raw_quotes, span_rows: list) -> list[dict]:
    """Keep <=3 verbatim-valid quotes. A quote is valid if its whitespace-stripped
    text (>= MIN_QUOTE_CHARS) is a substring of the span row it points to;
    otherwise relocate to the first span row that contains it (fixing msg_id);
    otherwise drop it. Sender is always overwritten with the DB row's, and the
    stored text is the DB row's own verbatim slice, not the LLM's copy."""
    if not isinstance(raw_quotes, list):
        return []
    span_by_id = {row["msg_id"]: row for row in span_rows}
    kept: list[dict] = []
    for quote in raw_quotes:
        if len(kept) >= MAX_QUOTES:
            break
        if not isinstance(quote, dict):
            continue
        text = quote.get("text")
        if not isinstance(text, str):
            continue
        needle = _strip_ws(text)
        if not (MIN_QUOTE_CHARS <= len(needle) <= MAX_QUOTE_CHARS):
            if needle:
                log.warning("growth quote dropped (length %d out of [%d,%d]): %r",
                            len(needle), MIN_QUOTE_CHARS, MAX_QUOTE_CHARS, text[:30])
            continue
        target = span_by_id.get(_as_int(quote.get("msg_id")))
        if target is not None and needle in _strip_ws(target["content"] or ""):
            match = target
        else:
            match = next(
                (r for r in span_rows if needle in _strip_ws(r["content"] or "")),
                None,
            )
            if match is None:
                log.warning("growth quote dropped (text not in span): %r", text[:30])
                continue
        kept.append({
            "msg_id": match["msg_id"],
            "sender": match["sender_name"] or "",
            "text": _verbatim_slice(match["content"] or "", needle) or text,
        })
    return kept


def _validate_segment(seg_dict, full_rows: list, full_by_id: dict,
                      cfg, date_str: str, chat_name: str):
    """Trust-boundary check of one proposed segment. Returns (GrowthSegment,
    span_rows) when it survives, else None (a warning is logged)."""
    if not isinstance(seg_dict, dict):
        return None
    start = _as_int(seg_dict.get("start_msg_id"))
    end = _as_int(seg_dict.get("end_msg_id"))
    if start is None or end is None:
        log.warning("growth segment dropped: non-integer msg-id bounds")
        return None
    if start not in full_by_id or end not in full_by_id or start > end:
        log.warning("growth segment dropped: msg-id span %s-%s not in day rows", start, end)
        return None

    span_rows = [r for r in full_rows if start <= r["msg_id"] <= end]
    count = len(span_rows)
    if count < cfg.growth.min_segment_msgs or count > cfg.growth.max_segment_msgs:
        log.warning("growth segment %s-%s dropped: %d msgs out of [%d,%d]",
                    start, end, count, cfg.growth.min_segment_msgs, cfg.growth.max_segment_msgs)
        return None

    first_ts = parse_timestamp(span_rows[0]["timestamp"])
    last_ts = parse_timestamp(span_rows[-1]["timestamp"])
    if last_ts - first_ts > timedelta(hours=MAX_SEGMENT_HOURS):
        log.warning("growth segment %s-%s dropped: spans %s > %dh",
                    start, end, last_ts - first_ts, MAX_SEGMENT_HOURS)
        return None

    score = _coerce_score(seg_dict.get("score"))
    quotes = _validate_quotes(seg_dict.get("quotes"), span_rows)
    status = "pending" if score >= cfg.growth.min_score else "rejected"
    if status == "pending" and not quotes:
        # 没有任何一句通过逐字校验的原话 = 卡片没有可信锚点，也往往是
        # 整段被 LLM 脑补的信号——降级存档，不排队推送。
        log.warning("growth segment %s-%s downgraded to rejected: no verbatim quote survived",
                    start, end)
        status = "rejected"
    seg = GrowthSegment(
        id=growth_store.segment_id(date_str, start),
        chat_id=span_rows[0]["chat_id"],
        chat_name=chat_name,
        date=date_str,
        start_msg_id=start,
        end_msg_id=end,
        start_hm=first_ts.astimezone(LOCAL_TZ).strftime("%H:%M"),
        end_hm=last_ts.astimezone(LOCAL_TZ).strftime("%H:%M"),
        msg_count=count,
        theme=str(seg_dict.get("theme") or "").strip()[:40],
        points=_clean_points(seg_dict.get("points")),
        quotes=quotes,
        participants=_participants(span_rows),
        score=score,
        status=status,
    )
    return seg, span_rows


def _chunk_rows(kept_rows: list, chunk_chars: int) -> list[list]:
    """Split the transcript rows into non-overlapping chunks. Once a chunk crosses
    `chunk_chars`, it closes at the next >=10min gap between consecutive kept
    messages; if no gap arrives by 1.5*chunk_chars it hard-splits."""
    if not kept_rows:
        return []
    hard_limit = chunk_chars * HARD_SPLIT_FACTOR
    gap = timedelta(minutes=GAP_SPLIT_MINUTES)
    chunks: list[list] = []
    current: list = []
    current_chars = 0
    prev_ts = None
    for row in kept_rows:
        ts = parse_timestamp(row["timestamp"])
        if current:
            if current_chars >= hard_limit:
                chunks.append(current)
                current, current_chars = [], 0
            elif current_chars >= chunk_chars and ts - prev_ts >= gap:
                chunks.append(current)
                current, current_chars = [], 0
        current.append(row)
        current_chars += len(_transcript_line(row)) + 1
        prev_ts = ts
    if current:
        chunks.append(current)
    return chunks


# ------------------------------------------------------------------- mine a day

def mine_day(llm, cfg, date_str: str, *, sync: bool = False,
             messages_db: Path | None = None,
             store_db: Path | None = None,
             segments_dir: Path | None = None) -> tuple[list[GrowthSegment], int]:
    """挖掘北京时间某一天的成长源群，返回 (本次新入库的段落, 去重前的候选段落数)。

    Defaults: messages_db = Path(cfg.sources.telegram.db_path).expanduser();
    store_db = paths.DB_PATH; segments_dir = paths.GROWTH_SEGMENTS_DIR."""
    if messages_db is None:
        messages_db = Path(cfg.sources.telegram.db_path).expanduser()
    if store_db is None:
        store_db = paths.DB_PATH
    if segments_dir is None:
        segments_dir = paths.GROWTH_SEGMENTS_DIR

    source = cfg.growth.source
    chat_id = _store_chat_id(source.id)
    if growth_store.day_already_mined(store_db, chat_id, date_str):
        log.info("growth day already mined, skipping: chat %s %s", chat_id, date_str)
        return [], 0

    if sync:
        sync_chat(source.id, limit=source.limit)

    until = (date.fromisoformat(date_str) + timedelta(days=1)).isoformat()
    full_rows = read_messages(db_path=messages_db, chat_id=source.id,
                              since=date_str, until=until, limit=source.limit)
    if not full_rows:
        growth_store.mark_day_mined(store_db, chat_id, date_str, 0)
        log.info("growth day has no messages: chat %s %s", chat_id, date_str)
        return [], 0

    full_by_id = {row["msg_id"]: row for row in full_rows}
    kept_rows = [r for r in full_rows if not should_skip_content(r["content"] or "")]
    chunks = _chunk_rows(kept_rows, cfg.growth.chunk_chars)

    validated: list[tuple[GrowthSegment, list]] = []
    failed = 0
    for idx, chunk in enumerate(chunks):
        prompt = _build_prompt(source.name, date_str, "\n".join(_transcript_line(r) for r in chunk))
        content, _usage = llm.chat(prompt, system=MINER_SYSTEM)
        try:
            data = _parse_llm_json(content, f"growth chunk {idx}")
        except ValueError as exc:
            log.warning("growth chunk %d of %s failed to parse: %s", idx, date_str, exc)
            failed += 1
            continue
        if not isinstance(data, dict) or not isinstance(data.get("segments"), list):
            log.warning("growth chunk %d of %s has no segments list", idx, date_str)
            failed += 1
            continue
        for seg_dict in data["segments"]:
            result = _validate_segment(seg_dict, full_rows, full_by_id, cfg, date_str, source.name)
            if result is not None:
                validated.append(result)

    # Store a prospective path before insertion so the persisted segment points
    # at its future archive, but only write the file AFTER overlap/exact-span
    # dedup tells us this candidate actually entered the queue.  Otherwise a
    # re-mine leaves an orphan slice that never appears in the DB index.
    spans_by_id: dict[str, list] = {}
    for seg, span_rows in validated:
        if seg.status == "pending":
            y, m, d = date_str.split("-")
            slice_path = Path(segments_dir) / y / m / f"{d}-{seg.start_msg_id}.md"
            seg.slice_path = str(slice_path)
            spans_by_id[seg.id] = span_rows

    inserted = growth_store.insert_segments(store_db, [seg for seg, _ in validated])
    for seg in inserted:
        if seg.status == "pending" and seg.slice_path:
            growth_store.write_slice_file(seg, spans_by_id[seg.id], Path(seg.slice_path))
    if any(seg.slice_path for seg in inserted):  # 只有落了切片才有索引可更新
        growth_store.regenerate_slice_index(store_db, segments_dir)

    if failed == 0:
        growth_store.mark_day_mined(store_db, chat_id, date_str, len(inserted))
    else:
        raise GrowthMiningError(
            f"growth mining {date_str}: {failed}/{len(chunks)} chunks failed, day not marked")

    return inserted, len(validated)
