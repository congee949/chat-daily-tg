from __future__ import annotations
from dataclasses import dataclass
import json
import logging
from pathlib import Path
import re

log = logging.getLogger(__name__)


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
    active_repeat_topics_summary: str = "",
) -> SummaryOutput:
    """Call LLM with summarization prompts and parse result."""
    from chat_daily_tg.prompts import SUMMARIZER_SYSTEM, build_user_prompt

    user_prompt = build_user_prompt(
        date=date,
        groups_with_content=groups_with_content,
        detail_path=detail_path,
        active_permanent_summary=active_permanent_summary,
        active_hot_leads_summary=active_hot_leads_summary,
        active_repeat_topics_summary=active_repeat_topics_summary,
    )
    content, _usage = llm_client.chat(user_prompt, system=SUMMARIZER_SYSTEM)
    try:
        return parse_summary_output(content)
    except ValueError as exc:
        raw_path = _raw_output_path(detail_path)
        raw_path.write_text(content, encoding="utf-8")
        log.warning("summary parse failed, saved raw output to %s: %s", raw_path, exc)
        repair_prompt = _build_repair_prompt(content, str(exc))
        repaired, _usage = llm_client.chat(repair_prompt, system=FORMAT_REPAIR_SYSTEM)
        return parse_summary_output(repaired)


FORMAT_REPAIR_SYSTEM = """你是一个严格的格式修复器。
只允许输出三个 fence，顺序固定：
```markdown concise
...
```

```markdown detailed
...
```

```json opportunities
...
```
不要解释，不要前言，不要后记。JSON 必须合法。不要新增事实，只重排和修复格式。
"""


def _build_repair_prompt(raw_output: str, error: str) -> str:
    return f"""下面是一次聊天日报 LLM 输出，但格式解析失败：{error}

请把它修复为严格的三段 fence 输出：
1. markdown concise
2. markdown detailed
3. json opportunities

如果原文缺少 opportunities JSON，请使用空数组：
{{"permanent_additions":[],"hot_leads_additions":[],"death_signals":[]}}

原始输出：
{raw_output}
"""


def _raw_output_path(detail_path: str) -> Path:
    path = Path(detail_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    return path.with_name("llm-output-unparsed.md")
