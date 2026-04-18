from __future__ import annotations
from dataclasses import dataclass
import json
import re


@dataclass(frozen=True)
class SummaryOutput:
    concise_md: str
    detailed_md: str
    opportunities: dict


_FENCE_RE = re.compile(r"```(\w+)\s+(\w+)\r?\n(.*?)```", re.DOTALL)


def parse_summary_output(text: str) -> SummaryOutput:
    """Parse the triple-fence LLM output into structured pieces.

    Expects fences in order: `markdown concise`, `markdown detailed`, `json opportunities`.
    Normalizes CRLF/CR line endings to LF before parsing.
    Raises ValueError on missing fences or malformed opportunities JSON.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    fences = {}
    for m in _FENCE_RE.finditer(text):
        lang, tag, body = m.group(1), m.group(2), m.group(3).strip()
        fences[(lang, tag)] = body
    required = [("markdown", "concise"), ("markdown", "detailed"), ("json", "opportunities")]
    for key in required:
        if key not in fences:
            raise ValueError(f"missing fence {key[0]} {key[1]}")
    try:
        opportunities = json.loads(fences[("json", "opportunities")])
    except json.JSONDecodeError as exc:
        raise ValueError(f"opportunities fence is not valid JSON: {exc}") from exc
    return SummaryOutput(
        concise_md=fences[("markdown", "concise")],
        detailed_md=fences[("markdown", "detailed")],
        opportunities=opportunities,
    )


def run_summary(
    llm_client,
    date: str,
    groups_with_content: list[tuple[str, str]],
    detail_path: str,
    active_permanent_summary: str = "",
    active_hot_leads_summary: str = "",
) -> SummaryOutput:
    """Call LLM with summarization prompts and parse result."""
    from wx_daily_tg.prompts import SUMMARIZER_SYSTEM, build_user_prompt

    user_prompt = build_user_prompt(
        date=date,
        groups_with_content=groups_with_content,
        detail_path=detail_path,
        active_permanent_summary=active_permanent_summary,
        active_hot_leads_summary=active_hot_leads_summary,
    )
    content, _usage = llm_client.chat(user_prompt, system=SUMMARIZER_SYSTEM)
    return parse_summary_output(content)
