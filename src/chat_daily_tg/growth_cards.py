"""Render growth-mining segments into Telegram-HTML cards (style A/B) and judge them.

Card A is deterministic (zero LLM): theme + bullet points + verbatim quotes.
Card B is narrative: one LLM call writes a short Chinese intro around the same
material; the quotes themselves are always code-assembled from `seg.quotes`
(never LLM-written) so the only untrusted text is the intro, which gets
sanitized before it reaches Telegram. `judge` makes one more LLM call to pick
a winner per a rubric, falling back to the zero-fabrication-risk card A on any
parse trouble — LLM output is a trust boundary, never assumed well-formed.
"""
from __future__ import annotations

import logging
import re

from chat_daily_tg.growth_store import GrowthSegment
from chat_daily_tg.summarizer import _safe_json_loads
from chat_daily_tg.tg_sender import escape_html

log = logging.getLogger(__name__)

# miner 让 LLM 用 **…** 在要点里标注关键短语；渲染层先转义再把成对标记转 <b>，
# 落单的 ** 直接剥掉。纯格式层，不改变要点文字本身。
_BOLD_MARK_RE = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
# 原话超过这个去空白长度就用可折叠引用块，长原文不刷屏。
_EXPANDABLE_QUOTE_CHARS = 120

_CARD_B_SYSTEM = "你是社群成长卡片的文案助手，只负责写一段引子，不负责核实事实。"

_CARD_B_PROMPT = """请根据下面的主题、要点和原话，写一段 2-4 句的中文引子，
把这段对话的背景和意义讲清楚，语气自然克制，像在给朋友做导读。

严格规则：
- 禁止编造任何新的事实、人名或数字——凡是没有出现在下面材料里的，一律不要提。
- 禁止使用「」引号造出新的引语句子。「」只用于逐字复述原话，你不需要也不允许自己写带「」的句子。
- 只输出这段引子本身，不要标题、前缀、后缀或解释。

主题：{theme}

要点：
{points_block}

原话：
{quotes_block}
"""

# 非贪婪匹配「...」括号跨度；DOTALL 防止引子里偶尔带换行的伪造引语漏检。
_QUOTE_SPAN_RE = re.compile(r"「(.*?)」", re.DOTALL)

_JUDGE_SYSTEM = "你是社群成长卡片的 A/B 评审员，严格按给定评审偏好打分，只输出 JSON。"

_JUDGE_PROMPT = """请依据下面的评审偏好，对比两版卡片文案，判断哪一版更好。

## 评审偏好
{rubric}

## 卡片 A
{card_a}

## 卡片 B
{card_b}

只输出下面这一行 JSON，不要任何解释、前言或代码块标记：
{{"winner": "A 或 B", "score_a": 0-10 之间的数字, "score_b": 0-10 之间的数字, "reason": "一句话理由"}}
"""

# style A 是零编造风险的默认项：judge 解析失败或输出越界时一律回落到 A。
_JUDGE_FALLBACK = {
    "winner": "A", "score_a": None, "score_b": None,
    "reason": "judge unavailable, deterministic fallback",
}


def build_footer(seg: GrowthSegment) -> str:
    """卡片统一落款：斜体的 内容日期 · 时间段——最小可溯源信息。

    日期必须有（存货卡的内容日期≠发送日期），时段区分同日多段；群名（单一
    来源纯重复）和 msg 区间（切片文件名与 DB slice_path 都有）不上卡。源群
    消息一天一清，没有可长期存活的 t.me 链接，原文回查靠本地切片。引用块
    自带视觉分隔，不再放 —————— 分隔线。"""
    return f"<i>📍 {seg.date} · {seg.start_hm}–{seg.end_hm}</i>"


def _render_point(text: str) -> str:
    """先转义再把成对 **…** 转 <b>，落单标记剥掉。"""
    rendered = _BOLD_MARK_RE.sub(r"<b>\1</b>", escape_html(text))
    return rendered.replace("**", "")


def _render_quote(quote: dict) -> str:
    """原生引用块渲染一条已校验原话；长原话用可折叠形态。"""
    tag = ("blockquote expandable"
           if len(_normalize_ws(quote["text"])) > _EXPANDABLE_QUOTE_CHARS
           else "blockquote")
    return (f"<{tag}>{escape_html(quote['text'])}\n"
            f"— {escape_html(quote['sender'])}</blockquote>")


def build_card_a(seg: GrowthSegment) -> str:
    """零 LLM、确定性卡片：主题 + 要点（关键词加粗）+ 原话引用块。
    原话为空则整段 💬 省略，不留空行。"""
    lines = [f"🌱 <b>{escape_html(seg.theme)}</b>", "", "📌 要点"]
    lines.extend(f"• {_render_point(p)}" for p in seg.points)
    if seg.quotes:
        lines.append("")
        lines.append("💬 原话")
        lines.append("".join(_render_quote(q) for q in seg.quotes))
    lines.append("")
    lines.append(build_footer(seg))
    return "\n".join(lines)


def _normalize_ws(text: str) -> str:
    return re.sub(r"\s+", "", text)


def _sanitize_intro(intro: str, quotes: list[dict]) -> str:
    """去掉 LLM 引子里的伪造「」引号。

    括号内文本（去空白后）不在已验证的原话集合里 —— 视为伪造引语，拆掉括号但保留文字；
    命中真实原话的（模型没听话但恰好一字不差抄对）保留括号，因为它本身是可信的。"""
    valid_texts = {_normalize_ws(q["text"]) for q in quotes}

    def _replace(m: re.Match) -> str:
        inner = m.group(1)
        return m.group(0) if _normalize_ws(inner) in valid_texts else inner

    return _QUOTE_SPAN_RE.sub(_replace, intro)


def build_card_b(llm, seg: GrowthSegment) -> str:
    """叙事卡片：最强原话开场（代码拼接，非 LLM）+ LLM 写的引子 + 其余原话 + 落款。

    LLM 异常直接向上抛出——调用方（发送流程）按 A 卡兜底。"""
    points_block = "\n".join(f"- {p}" for p in seg.points) if seg.points else "（无）"
    quotes_block = ("\n".join(f"「{q['text']}」— {q['sender']}" for q in seg.quotes)
                     if seg.quotes else "（无）")
    prompt = _CARD_B_PROMPT.format(theme=seg.theme, points_block=points_block,
                                    quotes_block=quotes_block)
    raw_intro, _usage = llm.chat(prompt, system=_CARD_B_SYSTEM)
    intro = escape_html(_sanitize_intro(raw_intro.strip(), seg.quotes))

    lines: list[str] = []
    remaining = seg.quotes
    if seg.quotes:
        lines.append(_render_quote(seg.quotes[0]))
        lines.append("")
        remaining = seg.quotes[1:]

    lines.append(intro)

    if remaining:
        lines.append("")
        lines.append("".join(_render_quote(q) for q in remaining))

    lines.append("")
    lines.append(build_footer(seg))
    return "\n".join(lines)


def _coerce_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def judge(llm, card_a: str, card_b: str, rubric_text: str) -> dict:
    """一次 LLM 调用，按 rubric_text 评审 A/B 两版卡片文案。

    信任边界：winner 不在 {"A","B"} 或输出解析失败，一律回落到零编造风险的 A 版
    （打分置空 + 记 warning）。"""
    prompt = _JUDGE_PROMPT.format(rubric=rubric_text, card_a=card_a, card_b=card_b)
    raw, _usage = llm.chat(prompt, system=_JUDGE_SYSTEM)

    try:
        parsed = _safe_json_loads(raw, "growth card judge")
    except ValueError as exc:
        log.warning("growth card judge output unparseable, falling back to A: %s", exc)
        return dict(_JUDGE_FALLBACK)

    if not isinstance(parsed, dict):
        log.warning("growth card judge output was not a JSON object (%r), falling back to A",
                    parsed)
        return dict(_JUDGE_FALLBACK)

    winner = parsed.get("winner")
    if winner not in ("A", "B"):
        log.warning("growth card judge returned invalid winner %r, falling back to A", winner)
        return dict(_JUDGE_FALLBACK)

    return {
        "winner": winner,
        "score_a": _coerce_float(parsed.get("score_a")),
        "score_b": _coerce_float(parsed.get("score_b")),
        "reason": parsed.get("reason", ""),
    }
