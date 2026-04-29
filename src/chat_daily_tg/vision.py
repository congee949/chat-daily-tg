from __future__ import annotations

from dataclasses import asdict, dataclass
import base64
import json
from pathlib import Path
from typing import Any

import httpx

from chat_daily_tg.media import MediaCandidate


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
            value_score=float(parsed.get("value_score") or 0.0),
            summary=str(parsed.get("summary") or ""),
            key_facts=[str(x) for x in parsed.get("key_facts") or []],
            risk_flags=[str(x) for x in parsed.get("risk_flags") or []],
            should_include_in_daily=bool(parsed.get("should_include_in_daily")),
            reason=str(parsed.get("reason") or ""),
        )


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
    min_include_score: float = 0.65,
) -> list[VisionAnalysis]:
    analyses: list[VisionAnalysis] = []
    for candidate in candidates:
        if candidate.score < min_prefilter_score or not candidate.local_path:
            continue
        try:
            analysis = client.analyze(candidate)
        except Exception:
            continue
        # Layer 3: OCR / empty image filter
        if _is_empty_vision(analysis):
            continue
        if analysis.value_score >= min_include_score:
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
