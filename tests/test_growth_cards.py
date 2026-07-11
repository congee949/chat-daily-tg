from __future__ import annotations

import logging

import pytest

from chat_daily_tg.growth_cards import (
    build_card_a,
    build_card_b,
    build_footer,
    judge,
)
from chat_daily_tg.growth_store import GrowthSegment

RUBRIC_TEXT = "评审偏好：金句必须是对话原话，观点要能迁移到我自己的处境。"


class FakeLLM:
    """Mirrors LLMClient.chat's signature — never touches the network."""

    def __init__(self, output: str):
        self.output = output
        self.calls: list[tuple[str, str | None]] = []

    def chat(self, prompt: str, system: str | None = None) -> tuple[str, dict]:
        self.calls.append((prompt, system))
        return self.output, {}


class BoomLLM:
    def chat(self, prompt: str, system: str | None = None) -> tuple[str, dict]:
        raise RuntimeError("llm boom")


def _seg(**overrides) -> GrowthSegment:
    base = dict(
        id="2026-07-10-1782515",
        chat_id=1162433032,
        chat_name="电丸朱氏会社",
        date="2026-07-10",
        start_msg_id=1782515,
        end_msg_id=1782652,
        start_hm="22:22",
        end_hm="23:07",
        msg_count=138,
        theme="回本心态与价值创造",
        points=["先想清楚为谁创造价值，再谈节省成本", "行动比空谈更重要"],
        quotes=[
            {"msg_id": 1782520, "sender": "A K", "text": "价值是创造出来的 不是节省出来的"},
            {"msg_id": 1782600, "sender": "J1mmy Ding", "text": "别光算账，先把事做成"},
        ],
        participants="A K, J1mmy Ding",
        score=8.5,
    )
    base.update(overrides)
    return GrowthSegment(**base)


# ---------------------------------------------------------------- build_footer

def test_build_footer_minimal_date_and_time_range():
    seg = _seg(chat_name="电丸<会社>&")
    footer = build_footer(seg)
    assert footer == "<i>📍 2026-07-10 · 22:22–23:07</i>"


# --------------------------------------------------------------- build_card_a

def test_build_card_a_golden_layout():
    seg = _seg()
    card = build_card_a(seg)
    expected = "\n".join([
        "🌱 <b>回本心态与价值创造</b>",
        "",
        "📌 要点",
        "• 先想清楚为谁创造价值，再谈节省成本",
        "• 行动比空谈更重要",
        "",
        "💬 原话",
        "<blockquote>价值是创造出来的 不是节省出来的\n— A K</blockquote>"
        "<blockquote>别光算账，先把事做成\n— J1mmy Ding</blockquote>",
        "",
        "<i>📍 2026-07-10 · 22:22–23:07</i>",
    ])
    assert card == expected


def test_build_card_a_bold_markers_and_expandable_quote():
    long_quote = "这是一条超过一百二十字的长原话" * 10  # 去空白后远超 120
    seg = _seg(
        points=["把注意力放在**开源**而非节流", "落单标记**保持文字不加粗"],
        quotes=[{"msg_id": 1, "sender": "A K", "text": long_quote}],
    )
    card = build_card_a(seg)
    assert "• 把注意力放在<b>开源</b>而非节流" in card       # 成对标记 → <b>
    assert "• 落单标记保持文字不加粗" in card                 # 落单 ** 剥掉
    assert f"<blockquote expandable>{long_quote}\n— A K</blockquote>" in card


def test_build_card_a_escapes_html_in_theme():
    seg = _seg(theme="<b>&</b>", points=["p"], quotes=[])
    card = build_card_a(seg)
    assert "🌱 <b>&lt;b&gt;&amp;&lt;/b&gt;</b>" in card


def test_build_card_a_empty_quotes_no_dangling_blank_lines():
    seg = _seg(quotes=[])
    card = build_card_a(seg)
    assert "💬 原话" not in card
    assert "\n\n\n" not in card
    expected = "\n".join([
        "🌱 <b>回本心态与价值创造</b>",
        "",
        "📌 要点",
        "• 先想清楚为谁创造价值，再谈节省成本",
        "• 行动比空谈更重要",
        "",
        "<i>📍 2026-07-10 · 22:22–23:07</i>",
    ])
    assert card == expected


# --------------------------------------------------------------- build_card_b

def test_build_card_b_sanitizes_fabricated_quote_keeps_real_opening_quote():
    seg = _seg()
    fake_intro = "这段讨论的核心是「假引用」，说明了行动力比空谈更重要。"
    llm = FakeLLM(fake_intro)

    card = build_card_b(llm, seg)

    assert len(llm.calls) == 1
    prompt, system = llm.calls[0]
    assert system is not None
    assert "回本心态与价值创造" in prompt

    # fabricated bracket span stripped, wording kept
    assert "「假引用」" not in card
    assert "假引用" in card
    # real opening quote (seg.quotes[0]) is a code-assembled blockquote
    opener = "<blockquote>价值是创造出来的 不是节省出来的\n— A K</blockquote>"
    remaining = "<blockquote>别光算账，先把事做成\n— J1mmy Ding</blockquote>"
    assert opener in card and remaining in card
    assert card.index(opener) < card.index("假引用") < card.index(remaining)
    assert card.endswith(build_footer(seg))


def test_build_card_b_empty_quotes_no_opening_quote():
    seg = _seg(quotes=[])
    llm = FakeLLM("这是一段不含引号的引子，讲清楚了背景与意义所在。")
    card = build_card_b(llm, seg)
    expected = "\n".join([
        "这是一段不含引号的引子，讲清楚了背景与意义所在。",
        "",
        build_footer(seg),
    ])
    assert card == expected


def test_build_card_b_escapes_html_in_quotes_and_intro():
    seg = _seg(quotes=[{"msg_id": 1, "sender": "A&B", "text": "先<做>后说"}])
    llm = FakeLLM("这段引子里 <script>alert(1)</script> 也应该被转义掉。")
    card = build_card_b(llm, seg)
    assert "<blockquote>先&lt;做&gt;后说\n— A&amp;B</blockquote>" in card
    assert "<script>" not in card
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in card


def test_build_card_b_propagates_llm_exception():
    seg = _seg()
    with pytest.raises(RuntimeError, match="llm boom"):
        build_card_b(BoomLLM(), seg)


# ---------------------------------------------------------------------- judge

def test_judge_happy_path_parses_winner_b_with_scores():
    llm = FakeLLM('{"winner": "B", "score_a": 6, "score_b": 8.5, "reason": "更贴近处境"}')
    result = judge(llm, "CARD A TEXT", "CARD B TEXT", RUBRIC_TEXT)
    assert result == {"winner": "B", "score_a": 6.0, "score_b": 8.5, "reason": "更贴近处境"}
    assert len(llm.calls) == 1
    prompt, system = llm.calls[0]
    assert system is not None
    assert "CARD A TEXT" in prompt
    assert "CARD B TEXT" in prompt
    assert RUBRIC_TEXT in prompt


def test_judge_garbage_output_falls_back_to_a(caplog):
    llm = FakeLLM("not json at all {{{")
    with caplog.at_level(logging.WARNING):
        result = judge(llm, "card A text", "card B text", RUBRIC_TEXT)
    assert result == {
        "winner": "A", "score_a": None, "score_b": None,
        "reason": "judge unavailable, deterministic fallback",
    }


def test_judge_invalid_winner_falls_back_to_a():
    llm = FakeLLM('{"winner": "C", "score_a": 5, "score_b": 5, "reason": "tie"}')
    result = judge(llm, "card A text", "card B text", RUBRIC_TEXT)
    assert result == {
        "winner": "A", "score_a": None, "score_b": None,
        "reason": "judge unavailable, deterministic fallback",
    }
