from __future__ import annotations

from dataclasses import asdict, dataclass
import base64
import json
import logging
import re
from pathlib import Path
from typing import Any

import httpx

from chat_daily_tg.media import MediaCandidate

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class VisionAnalysis:
    candidate: MediaCandidate
    type: str
    value_score: float
    summary: str
    key_facts: list[str]
    risk_flags: list[str]
    should_include_in_daily: bool
    reason: str

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["candidate"] = self.candidate.to_json()
        return data


class VisionClient:
    def __init__(self, *, endpoint: str, model: str, api_key: str, timeout: float = 120.0):
        self.endpoint = endpoint
        self.model = model
        self.api_key = api_key
        self.timeout = timeout

    def analyze(self, candidate: MediaCandidate) -> VisionAnalysis:
        if not candidate.local_path:
            raise ValueError("vision analysis requires a local image path")
        image_url = _image_data_url(Path(candidate.local_path).expanduser())
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": _vision_prompt(candidate)},
                        {"type": "image_url", "image_url": {"url": image_url}},
                    ],
                }
            ],
            "max_tokens": 1200,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        with httpx.Client(timeout=self.timeout) as client:
            response = client.post(f"{self.endpoint}/chat/completions", json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()
        content = data["choices"][0]["message"]["content"]
        parsed = _parse_json_object(content)
        return VisionAnalysis(
            candidate=candidate,
            type=str(parsed.get("type") or "unknown"),
            value_score=_normalize_score(parsed.get("value_score")),
            summary=str(parsed.get("summary") or ""),
            key_facts=[str(x) for x in parsed.get("key_facts") or []],
            risk_flags=[str(x) for x in parsed.get("risk_flags") or []],
            should_include_in_daily=_coerce_include_flag(parsed.get("should_include_in_daily")),
            reason=str(parsed.get("reason") or ""),
        )


def _normalize_score(raw) -> float:
    """Coerce value_score into [0, 1]. LLMs drift off the requested 0-1 scale
    despite the prompt (qwen returned 2.5/3.0; gemini returned 8.5 on a 0-10
    scale) — a score in (1, 10] is treated as a 0-10 rating and divided by 10.
    Anything still outside [0, 1] afterwards (negative, >10, non-numeric) is
    untrustworthy and coerced to 0.0 so it drops below the include-bar, rather
    than clamped up to 1.0 which would promote a garbage rating into a top image."""
    try:
        score = float(raw or 0.0)
    except (TypeError, ValueError):
        log.warning("vision value_score not numeric (%r); treating as 0.0", raw)
        return 0.0
    if 1.0 < score <= 10.0:
        score = score / 10.0
    if not 0.0 <= score <= 1.0:
        log.warning("vision value_score out of range (%r); treating as 0.0", raw)
        return 0.0
    return score


def _coerce_include_flag(raw) -> bool:
    """should_include_in_daily lets the model veto a high-scoring image out of the
    digest. Only an explicit JSON boolean is trusted; a missing or non-bool value
    falls back to True so the decision reduces to the score gate alone — the field
    being absent must not silently start excluding images."""
    if isinstance(raw, bool):
        return raw
    return True


def _is_empty_vision(analysis: VisionAnalysis) -> bool:
    """Filter out images that are memes, pure selfies, or have no extractable text/info."""
    summary = (analysis.summary or "").lower()
    if analysis.type in ("meme", "unknown") and analysis.value_score < 0.75:
        return True
    if not analysis.key_facts and analysis.value_score < 0.55:
        return True
    if "表情包" in summary or "无文字" in summary or "纯图片" in summary:
        return True
    return False


def analyze_media_candidates(
    *,
    client: VisionClient,
    candidates: list[MediaCandidate],
    min_prefilter_score: float = 0.45,
    min_include_score: float = 0.8,
) -> list[VisionAnalysis]:
    """min_include_score=0.8: only clearly high-value images reach the digest
    (raised from 0.65 — user feedback 2026-07-02)."""
    from chat_daily_tg.media import _is_valid_image_file
    analyses: list[VisionAnalysis] = []
    for candidate in candidates:
        if candidate.score < min_prefilter_score or not candidate.local_path:
            continue
        # Thumbnail/quality gate: WeChat's local cache often holds only a
        # 96x210 thumbnail (wx extract can't get more unless the original was
        # viewed on-device) — a blurry thumb must never reach vision or the
        # digest, so require real-image size AND resolution here.
        ok, _reason = _is_valid_image_file(candidate.local_path)
        if not ok:
            continue
        try:
            analysis = client.analyze(candidate)
        except Exception:
            continue
        # Layer 3: OCR / empty image filter
        if _is_empty_vision(analysis):
            continue
        # Include-bar: high enough value AND the model didn't veto inclusion.
        if analysis.value_score >= min_include_score and analysis.should_include_in_daily:
            analyses.append(analysis)
    return analyses


def vision_markdown(analyses: list[VisionAnalysis]) -> str:
    if not analyses:
        return ""
    lines = ["# 图片理解结果", ""]
    for item in analyses:
        c = item.candidate
        lines.append(f"## {c.group_name} / {c.timestamp} / {c.sender_name}")
        lines.append(f"- 类型：{item.type}")
        lines.append(f"- 价值分：{item.value_score:.2f}")
        lines.append(f"- 摘要：{item.summary}")
        if item.key_facts:
            lines.append("- 关键信息：" + "；".join(item.key_facts))
        if item.risk_flags:
            lines.append("- 风险： " + "；".join(item.risk_flags))
        lines.append(f"- 判断：{'进入日报' if item.should_include_in_daily else '仅归档'}，{item.reason}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


MAX_CITATIONS = 5  # cap so the digest doesn't turn into a photo slideshow

_CITATION_RE = re.compile(r"\[IMG(\d+)\]")


def build_citation_block(analyses: list[VisionAnalysis]) -> tuple[str, dict[int, VisionAnalysis]]:
    """Top MAX_CITATIONS analyses, presented to the summary LLM as a numbered,
    citable list. Returns (markdown_block, id_map); id_map is used by
    resolve_citations() to turn any [IMGn] markers the LLM emits back into images.

    Telegram-sourced images rank ahead of WeChat ones at equal footing: TG photos
    are full-size originals (≥1280px typically), while WeChat's local cache often
    yields lower-quality files — the user prefers sharp TG shots in the digest."""
    ranked = sorted(
        analyses,
        key=lambda a: (a.candidate.platform == "Telegram", a.value_score),
        reverse=True,
    )[:MAX_CITATIONS]
    if not ranked:
        return "", {}
    id_map = {i + 1: item for i, item in enumerate(ranked)}
    lines = [
        "# 可引用图片",
        "",
        "如果下面某张截图能直接印证某条重点，在那条 bullet 末尾追加引用标记 "
        "`[IMGn]`（n 用下面列出的编号）；全文最多引用 3 张，优先选择 AI/工具 "
        "相关的截图；同等相关时优先引用 Telegram 来源的图（清晰度更高）；"
        "只在截图确实直接印证内容时才引用，没有合适的图就不要引用，"
        "禁止编造列表之外的编号。",
        "",
    ]
    for cid, item in id_map.items():
        c = item.candidate
        lines.append(f"## [IMG{cid}] {c.platform} / {c.group_name} / {c.timestamp}")
        lines.append(f"- 摘要：{item.summary}")
        if item.key_facts:
            lines.append("- 关键信息：" + "；".join(item.key_facts))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n", id_map


# Section heading that marks the AI/tools bucket; a citation there wins the
# max_images cut (user preference: the one inserted image should be AI-related).
_AI_SECTION_RE = re.compile(r"^###[^\n]*(?:AI|工具)[^\n]*$", re.MULTILINE)
_SECTION_HEAD_RE = re.compile(r"^### ", re.MULTILINE)


def resolve_citations(
    text: str, id_map: dict[int, VisionAnalysis], max_images: int = 3,
) -> list[tuple[str, VisionAnalysis | None]]:
    """Split text on [IMGn] markers into ordered (text_chunk, image_or_None) segments.

    Unknown ids (LLM hallucination, or a candidate whose local_path is missing) are
    stripped from the text rather than leaked as a raw bracket token, without
    breaking the segment there.

    At most max_images markers survive (each photo is its own Telegram message,
    and the user wants the digest to stay in large intact blocks, not fragment
    into a message per image). When trimming, a marker inside the AI/工具
    section beats document order; the prompt asks the LLM for a single
    AI-preferred citation already — this is the code-level backstop.
    """
    valid = {cid: item for cid, item in id_map.items() if item.candidate.local_path}
    text = _CITATION_RE.sub(lambda m: m.group(0) if int(m.group(1)) in valid else "", text)
    matches = list(_CITATION_RE.finditer(text))
    if len(matches) > max_images:
        in_ai: list[re.Match] = []
        heading = _AI_SECTION_RE.search(text)
        if heading:
            nxt = _SECTION_HEAD_RE.search(text, heading.end())
            section_end = nxt.start() if nxt else len(text)
            in_ai = [m for m in matches if heading.end() <= m.start() < section_end]
        keep = (in_ai + [m for m in matches if m not in in_ai])[:max_images]
        keep_spans = {m.span() for m in keep}
        text = _CITATION_RE.sub(lambda m: m.group(0) if m.span() in keep_spans else "", text)
    segments: list[tuple[str, VisionAnalysis | None]] = []
    last_end = 0
    for m in _CITATION_RE.finditer(text):
        segments.append((text[last_end:m.start()], valid[int(m.group(1))]))
        last_end = m.end()
    tail = text[last_end:]
    if tail.strip() or not segments:
        segments.append((tail, None))
    return segments


def write_vision_analyses(path: Path, analyses: list[VisionAnalysis]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for item in analyses:
            f.write(json.dumps(item.to_json(), ensure_ascii=False) + "\n")


def _vision_prompt(candidate: MediaCandidate) -> str:
    return f"""请判断这张聊天图片是否有日报价值，只输出 JSON 对象，不要 Markdown。

来源：{candidate.platform} / {candidate.group_name} / {candidate.timestamp} / {candidate.sender_name}
上下文：
{candidate.context}

输出字段：
{{
  "type": "activity_poster|price_screenshot|risk_screenshot|tutorial|chat_screenshot|meme|unknown",
  "value_score": 0.0,
  "summary": "...",
  "key_facts": ["..."],
  "risk_flags": ["..."],
  "should_include_in_daily": false,
  "reason": "..."
}}

value_score 必须是 0.0 到 1.0 之间的小数（1.0 = 极高日报价值），不要使用 0-10 制。
"""


def _image_data_url(path: Path) -> str:
    data = path.read_bytes()
    mime = _mime_type(path)
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def _mime_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".png":
        return "image/png"
    if suffix == ".webp":
        return "image/webp"
    return "image/jpeg"


def _parse_json_object(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    return json.loads(stripped)
