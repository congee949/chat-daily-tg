from __future__ import annotations
from dataclasses import dataclass
import json
import logging
from pathlib import Path
import re
from typing import Callable

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SummaryOutput:
    concise_md: str
    detailed_md: str
    opportunities: dict
    verification: dict | None = None


# A fence opener line carries an info string: ```lang [tag]. A closer line is
# a bare ```. Parsing line-by-line with a depth counter (instead of a
# non-greedy `.*?` regex) keeps a body's own ```python code block from
# prematurely terminating the outer fence (review finding #37).
_FENCE_OPEN_RE = re.compile(r"^```(\w+)(?:\s+(\w+))?\s*$")
_FENCE_BARE_RE = re.compile(r"^```\s*$")

# The only structural top-level fences this format emits. Their openers are
# unambiguous (no inner code block is ```markdown concise etc.), so they act as
# hard block boundaries: even if a body contains an UNBALANCED inner fence (an
# odd ``` that never closes), the next structural opener ends the current block
# instead of letting it swallow the following blocks (verification self-review).
_KNOWN_TOPLEVEL = {
    ("markdown", "concise"), ("markdown", "detailed"),
    ("json", "opportunities"), ("json", "verification"),
}


def _extract_fences(text: str) -> dict[tuple[str, str], str]:
    """Extract top-level fenced blocks keyed by (lang, tag).

    Balanced nested code blocks inside a body are preserved (depth tracking); an
    unbalanced inner fence cannot swallow a following structural block (a known
    top-level opener bounds it); a truncated final fence yields whatever was
    captured before EOF. Last occurrence of a key wins — this restores the
    pre-rewrite semantics the untagged-json verification fallback relies on when
    both opportunities and verification are emitted as bare ```json.
    """
    fences: dict[tuple[str, str], str] = {}
    lines = text.split("\n")
    i, n = 0, len(lines)
    while i < n:
        m = _FENCE_OPEN_RE.match(lines[i])
        if not m:
            i += 1
            continue
        key = (m.group(1), m.group(2) or "")
        i += 1
        body_lines: list[str] = []
        depth = 1
        while i < n and depth > 0:
            line = lines[i]
            mm = _FENCE_OPEN_RE.match(line)
            if mm and (mm.group(1), mm.group(2) or "") in _KNOWN_TOPLEVEL:
                # Next structural block begins — stop without consuming this line
                # so the outer loop reprocesses it (handles a missing/unbalanced
                # closer on the current block).
                break
            if _FENCE_BARE_RE.match(line):
                depth -= 1
                if depth == 0:
                    i += 1
                    break
                body_lines.append(line)
            else:
                if mm:
                    depth += 1
                body_lines.append(line)
            i += 1
        fences[key] = "\n".join(body_lines).strip()
    return fences


def _sanitize_json(raw: str) -> str:
    """Fix common LLM JSON output issues before parsing."""
    # Fix invalid \uXXXX escapes: \u not followed by 4 hex digits
    raw = re.sub(r"\\u(?![0-9a-fA-F]{4})", r"\\\\u", raw)
    # Remove trailing commas before } or ]
    raw = re.sub(r",\s*([}\]])", r"\1", raw)
    # Try parsing as-is first
    try:
        json.loads(raw)
        return raw
    except json.JSONDecodeError:
        pass
    # Attempt to close unclosed braces/brackets
    opens = raw.count("{") - raw.count("}")
    brackets = raw.count("[") - raw.count("]")
    if opens > 0 or brackets > 0:
        candidate = raw.rstrip().rstrip(",")
        candidate += "]" * brackets + "}" * opens
        try:
            json.loads(candidate)
            return candidate
        except json.JSONDecodeError:
            pass
    return raw


def _safe_json_loads(raw: str, label: str) -> dict:
    """Parse JSON with sanitization fallback."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    sanitized = _sanitize_json(raw)
    try:
        return json.loads(sanitized)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} is not valid JSON: {exc}") from exc


def parse_summary_output(text: str) -> SummaryOutput:
    """Parse the triple-fence LLM output into structured pieces.

    Expects fences in order: `markdown concise`, `markdown detailed`, `json opportunities`.
    Tolerates truncated output (unclosed fences) — only concise is strictly required.
    Normalizes CRLF/CR line endings to LF before parsing.
    Raises ValueError if concise fence is missing.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    fences = _extract_fences(text)
    if ("markdown", "concise") not in fences:
        raise ValueError("missing fence markdown concise")
    concise = fences[("markdown", "concise")]
    detailed = fences.get(("markdown", "detailed"), "")
    raw_opps = fences.get(("json", "opportunities"))
    if raw_opps is not None:
        opportunities = _safe_json_loads(raw_opps, "opportunities fence")
    else:
        opportunities = {"permanent_additions": [], "hot_leads_additions": [], "death_signals": []}
    return SummaryOutput(
        concise_md=concise,
        detailed_md=detailed,
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
    cross_group_cluster_text: str = "",
    evidence_context: str = "",
    evidence_context_builder: Callable[[SummaryOutput], str] | None = None,
) -> SummaryOutput:
    """Call LLM with summarization prompts, verify claims, and parse result."""
    from chat_daily_tg.prompts import SUMMARIZER_SYSTEM, build_user_prompt

    user_prompt = build_user_prompt(
        date=date,
        groups_with_content=groups_with_content,
        detail_path=detail_path,
        active_permanent_summary=active_permanent_summary,
        active_hot_leads_summary=active_hot_leads_summary,
        active_repeat_topics_summary=active_repeat_topics_summary,
        cross_group_cluster_text=cross_group_cluster_text,
    )
    content, _usage = llm_client.chat(user_prompt, system=SUMMARIZER_SYSTEM)
    initial = _parse_or_repair_summary(llm_client, content, detail_path)
    # Preserve the pre-verification draft on disk. The verifier rewrites the
    # body wholesale, so if it hallucinates a removed claim the original text is
    # always recoverable (review finding #39).
    _persist_initial_draft(detail_path, initial)
    verifier_sources = groups_with_content
    if evidence_context_builder is not None:
        evidence_context = evidence_context_builder(initial)
        if not evidence_context.strip():
            # Empty context means extract_claim_queries found zero high-risk
            # claims in the draft — the verifier's mandate is exactly that
            # claim list, so the second LLM call has nothing to check.
            log.info("no high-risk claims in draft, skipping verifier LLM call")
            return SummaryOutput(
                concise_md=initial.concise_md,
                detailed_md=initial.detailed_md,
                opportunities=initial.opportunities,
                verification={"checked_claims": []},
            )
        # The evidence builder extracts high-risk claims from the draft and
        # retrieves their local message windows.  Giving the verifier the full
        # day of raw chat *as well* duplicated the largest prompt in the run.
        # For this scoped mode, candidate evidence is the only factual input;
        # the verifier must leave claims it cannot support untouched rather
        # than invent a rewrite from unrelated chat context.
        verifier_sources = []
    verified_content, _usage = llm_client.chat(
        _build_verifier_prompt(
            date=date,
            groups_with_content=verifier_sources,
            draft_output=_render_summary_output(initial),
            evidence_context=evidence_context,
        ),
        system=VERIFIER_SYSTEM,
    )
    try:
        return _parse_verified_output(llm_client, verified_content, detail_path)
    except ValueError as exc:
        # Verification is an enhancement, not a publish gate. If its output (and
        # the repair of it) can't be parsed, ship the already-good initial draft
        # rather than dropping the whole day's report (review finding #34).
        log.warning("verifier output unparseable (%s); publishing unverified initial draft", exc)
        return SummaryOutput(
            concise_md=initial.concise_md,
            detailed_md=initial.detailed_md,
            opportunities=initial.opportunities,
            verification={"error": "verifier_parse_failed"},
        )


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

VERIFIER_SYSTEM = """你是聊天日报事实核验器，只根据用户提供的原始聊天记录或检索证据审核日报初稿。

目标：防止把模糊指代、缺失主语、相邻上下文里的品牌名误补成确定事实。

必须逐条检查精简版和详细版中的高风险 claim：
- 产品/模型/公司/政策/活动的发布、涨价、封禁、退出、额度变化
- 带具体版本号、品牌名、机构名、金额、日期、链接归属的结论
- 跨群验证、官方确认、第一名、榜单、LiveBench 等强事实表述

核验规则：
- 只有原始聊天记录明确写出实体名，才能在结论中保留该实体名。
- 原文只写“4.3”“这个”“新出的”“它”等省略说法时，禁止补成 Claude、Grok、GPT 等具体产品名。
- 原文有线索但没有明文主语时，改写为“疑似某 4.3 模型/工具”，并放入“风险 / 待验证”。
- 原文支持事实但来源弱、传闻、单人猜测时，保留但标为“待验证”或“群友反馈”。
- 如果初稿把相邻话题的品牌错贴到当前 claim，必须删除或降级。
- 不要引入原始聊天之外的新事实，不要联网，不要凭常识补全。
- 金额必须保留原文的货币单位和语境；原文没有货币符号时，禁止自行添加 $、€ 等符号或改变币种。
- 当输入只提供“检索证据”而没有全量聊天记录时，只核验检索覆盖的高风险 claim；没有证据的其他段落必须原样保留，不能基于常识改写。

输出要求：四个 fence，顺序固定：
```markdown concise
...
```

```markdown detailed
...
```

```json opportunities
...
```

```json verification
{
  "checked_claims": [
    {
      "claim": "...",
      "status": "supported|downgraded|removed|needs_verification",
      "reason": "...",
      "evidence": ["原始聊天中的短引文，含群名/时间"],
      "confidence": "high|medium|low"
    }
  ]
}
```
不要解释，不要前言，不要后记。
"""


def _parse_or_repair_summary(llm_client, content: str, detail_path: str) -> SummaryOutput:
    try:
        return parse_summary_output(content)
    except ValueError as exc:
        raw_path = _raw_output_path(detail_path)
        raw_path.write_text(content, encoding="utf-8")
        log.warning("summary parse failed, saved raw output to %s: %s", raw_path, exc)
        repair_prompt = _build_repair_prompt(content, str(exc))
        repaired, _usage = llm_client.chat(repair_prompt, system=FORMAT_REPAIR_SYSTEM)
        try:
            return parse_summary_output(repaired)
        except ValueError as exc2:
            # Repair also failed. Rather than crash the whole day's pipeline,
            # salvage whatever concise body the original draft contained
            # (review finding #33). Only a draft with no usable concise is fatal.
            log.warning("repair output still unparseable (%s); attempting best-effort salvage", exc2)
            fallback = _best_effort_summary(content) or _best_effort_summary(repaired)
            if fallback is not None:
                log.warning("salvaged concise body from unparseable draft")
                return fallback
            raise


def _best_effort_summary(content: str) -> SummaryOutput | None:
    """Extract whatever concise body is recoverable; None if none usable."""
    text = content.replace("\r\n", "\n").replace("\r", "\n")
    fences = _extract_fences(text)
    concise = fences.get(("markdown", "concise"), "").strip()
    if not concise:
        return None
    raw_opps = fences.get(("json", "opportunities"))
    opportunities = {"permanent_additions": [], "hot_leads_additions": [], "death_signals": []}
    if raw_opps:
        try:
            opportunities = _safe_json_loads(raw_opps, "opportunities fence")
        except ValueError:
            pass
    return SummaryOutput(
        concise_md=concise,
        detailed_md=fences.get(("markdown", "detailed"), ""),
        opportunities=opportunities,
    )


def _persist_initial_draft(detail_path: str, initial: SummaryOutput) -> None:
    """Best-effort: save the pre-verification draft next to the summary."""
    try:
        base = Path(detail_path).expanduser()
        base.parent.mkdir(parents=True, exist_ok=True)
        base.with_name("initial-concise.md").write_text(initial.concise_md, encoding="utf-8")
        base.with_name("initial-detailed.md").write_text(initial.detailed_md, encoding="utf-8")
    except OSError as exc:
        log.warning("could not persist initial draft: %s", exc)


def _parse_verified_output(llm_client, content: str, detail_path: str) -> SummaryOutput:
    try:
        return parse_verified_summary_output(content)
    except ValueError as exc:
        raw_path = _verified_raw_output_path(detail_path)
        raw_path.write_text(content, encoding="utf-8")
        log.warning("verified summary parse failed, saved raw output to %s: %s", raw_path, exc)
        repair_prompt = _build_verified_repair_prompt(content, str(exc))
        repaired, _usage = llm_client.chat(repair_prompt, system=VERIFIED_FORMAT_REPAIR_SYSTEM)
        return parse_verified_summary_output(repaired)


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


VERIFIED_FORMAT_REPAIR_SYSTEM = """你是一个严格的格式修复器。
只允许输出四个 fence，顺序固定：
```markdown concise
...
```

```markdown detailed
...
```

```json opportunities
...
```

```json verification
...
```
不要解释，不要前言，不要后记。JSON 必须合法。不要新增事实，只重排和修复格式。
"""


def parse_verified_summary_output(text: str) -> SummaryOutput:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    fences = _extract_fences(text)
    output = parse_summary_output(text)
    raw_verification = fences.get(("json", "verification"))
    if raw_verification is None:
        raw_verification = _find_untagged_verification_fence(fences)
    if raw_verification is None:
        raise ValueError("missing fence json verification")
    verification = _safe_json_loads(raw_verification, "verification fence")
    return SummaryOutput(
        concise_md=output.concise_md,
        detailed_md=output.detailed_md,
        opportunities=output.opportunities,
        verification=verification,
    )


def _find_untagged_verification_fence(fences: dict[tuple[str, str], str]) -> str | None:
    for (lang, tag), raw in fences.items():
        if lang != "json" or tag != "":
            continue
        try:
            parsed = _safe_json_loads(raw, "untagged json fence")
        except ValueError:
            continue
        if "checked_claims" in parsed:
            return raw
    return None


def _build_verified_repair_prompt(raw_output: str, error: str) -> str:
    return f"""下面是一次聊天日报核验器输出，但格式解析失败：{error}

请把它修复为严格的四段 fence 输出：
1. markdown concise
2. markdown detailed
3. json opportunities
4. json verification

如果原文缺少 verification JSON，请使用：
{{"checked_claims":[]}}

如果原文缺少 opportunities JSON，请使用：
{{"permanent_additions":[],"hot_leads_additions":[],"death_signals":[]}}

原始输出：
{raw_output}
"""


def _build_verifier_prompt(
    *,
    date: str,
    groups_with_content: list[tuple[str, str]],
    draft_output: str,
    evidence_context: str = "",
) -> str:
    groups_block = "\n\n".join(
        f"### === 来源: {name} ===\n\n{content}"
        for name, content in groups_with_content
    )
    source_section = f"""## 原始聊天记录

{groups_block}
""" if groups_block else """## 原始聊天记录

本次未附全量聊天记录；仅可依据下面的检索证据核验其覆盖的高风险 claim。
"""
    evidence_section = f"""
## Embedding 检索证据

下面是从本地向量索引按高风险 claim 检索出的候选证据。它们只是候选，不代表事实成立；你必须继续检查原始聊天记录。

{evidence_context}
""" if evidence_context.strip() else ""
    return f"""日期：{date}

{source_section}{evidence_section}
## 日报初稿

{draft_output}

请审核初稿中的事实 claim，修正无证据实体补全和主语错贴，输出修正后的四段 fence。
"""


def _render_summary_output(output: SummaryOutput) -> str:
    return "\n\n".join([
        f"```markdown concise\n{output.concise_md}\n```",
        f"```markdown detailed\n{output.detailed_md}\n```",
        "```json opportunities\n"
        f"{json.dumps(output.opportunities, ensure_ascii=False, indent=2)}\n"
        "```",
    ])


def _raw_output_path(detail_path: str) -> Path:
    path = Path(detail_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    return path.with_name("llm-output-unparsed.md")


def _verified_raw_output_path(detail_path: str) -> Path:
    path = Path(detail_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    return path.with_name("llm-output-verified-unparsed.md")
