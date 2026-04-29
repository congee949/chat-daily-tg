from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
import re
from typing import Any


VALUE_KEYWORDS = (
    "活动", "价格", "额度", "bug", "风控", "封号", "报错", "教程", "入口",
    "二维码", "付款", "返现", "优惠", "羊毛", "截图", "表格", "评测", "模型",
    "账号", "冻结", "补件", "验证", "链接", "官方", "实测",
)
LOW_VALUE_KEYWORDS = ("哈哈", "笑死", "表情", "梗图", "好图", "收藏", "自拍")


@dataclass(frozen=True)
class MediaCandidate:
    platform: str
    group_name: str
    timestamp: str
    sender_name: str
    media_type: str
    local_path: str | None
    context: str
    reason: str
    score: float
    raw_ref: str | None = None

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


_WX_TS_RE = re.compile(r"^### (?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2})$", re.MULTILINE)
_WX_IMAGE_RE = re.compile(r"\[(?P<kind>图片|视频|文件|链接卡片|小程序)\]\s*local_id=(?P<id>\d+)")
_WX_SENDER_RE = re.compile(r"\*\*(?P<sender>[^*]+)\*\*:")


def media_markdown(candidates: list[MediaCandidate]) -> str:
    if not candidates:
        return ""
    lines = ["# 图片/媒体候选", ""]
    for item in candidates:
        path = item.local_path or "无本地路径"
        lines.append(
            f"- [{item.platform} / {item.group_name} / {item.timestamp} / {item.sender_name}] "
            f"{item.media_type} score={item.score:.2f} reason={item.reason} path={path}"
        )
    return "\n".join(lines) + "\n"


def write_media_candidates(path: Path, candidates: list[MediaCandidate]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for item in candidates:
            f.write(json.dumps(item.to_json(), ensure_ascii=False) + "\n")


def _is_valid_image_file(path: str | None) -> tuple[bool, str]:
    """Layer 1 prefilter: file size and resolution checks."""
    if not path:
        return True, "no local path"
    p = Path(path).expanduser()
    if not p.exists():
        return False, "file not found"
    size = p.stat().st_size
    if size < 10 * 1024:
        return False, f"too small ({size}B < 10KB)"
    if size > 20 * 1024 * 1024:
        return False, f"too large ({size // (1024*1024)}MB > 20MB)"
    try:
        from PIL import Image as PILImage
        with PILImage.open(p) as img:
            w, h = img.size
            if w < 300 or h < 300:
                return False, f"too small resolution ({w}x{h})"
    except Exception:
        pass
    return True, "pass"


def score_media_context(context: str, *, has_local_path: bool = False) -> tuple[float, str]:
    text = context.lower()
    hits = [kw for kw in VALUE_KEYWORDS if kw.lower() in text]
    low_hits = [kw for kw in LOW_VALUE_KEYWORDS if kw.lower() in text]
    score = 0.2 + min(len(hits), 5) * 0.14
    if has_local_path:
        score += 0.08
    if low_hits and not hits:
        score -= 0.2
    score = max(0.0, min(score, 1.0))
    if hits:
        reason = "上下文命中高价值关键词：" + "、".join(hits[:5])
    elif low_hits:
        reason = "上下文更像低信息图片：" + "、".join(low_hits[:3])
    else:
        reason = "上下文缺少明显价值信号"
    return score, reason


def extract_wx_media_candidates(raw_markdown: str, *, group_name: str) -> list[MediaCandidate]:
    matches = list(_WX_TS_RE.finditer(raw_markdown))
    candidates: list[MediaCandidate] = []
    for idx, match in enumerate(matches):
        start = match.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(raw_markdown)
        block = raw_markdown[start:end].strip()
        if not _WX_IMAGE_RE.search(block):
            continue
        sender = _extract_sender(block)
        context = _nearby_context(raw_markdown, match.start(), end)
        score, reason = score_media_context(context)
        for media in _WX_IMAGE_RE.finditer(block):
            candidates.append(MediaCandidate(
                platform="微信",
                group_name=group_name,
                timestamp=match.group("ts"),
                sender_name=sender,
                media_type=media.group("kind"),
                local_path=None,
                context=context,
                reason=reason,
                score=score,
                raw_ref=f"local_id={media.group('id')}",
            ))
    return candidates


def extract_telegram_media_candidates(rows: list[Any], *, fallback_chat_name: str) -> list[MediaCandidate]:
    candidates: list[MediaCandidate] = []
    for idx, row in enumerate(rows):
        raw_json = row["raw_json"] if row["raw_json"] else ""
        content = row["content"] or ""
        paths = media_paths_from_raw_json(raw_json)
        if not paths and not _looks_like_media_json(raw_json):
            continue
        # Layer 1 prefilter
        local_path = paths[0] if paths else None
        ok, filter_reason = _is_valid_image_file(local_path)
        if not ok:
            continue
        context = _telegram_context(rows, idx)
        score, reason = score_media_context(context, has_local_path=bool(paths))
        candidates.append(MediaCandidate(
            platform="Telegram",
            group_name=row["chat_name"] or fallback_chat_name,
            timestamp=str(row["timestamp"]),
            sender_name=row["sender_name"] or "unknown",
            media_type=_media_type_from_raw_json(raw_json),
            local_path=local_path,
            context=context or content,
            reason=reason,
            score=score,
            raw_ref=f"msg_id={row['msg_id']}",
        ))
    return candidates


def media_paths_from_raw_json(raw_json: str) -> list[str]:
    if not raw_json:
        return []
    try:
        data = json.loads(raw_json)
    except json.JSONDecodeError:
        return []
    out: list[str] = []
    for value in _walk_json(data):
        if not isinstance(value, str):
            continue
        if _is_image_path(value) and Path(value).expanduser().exists():
            out.append(value)
    return out


def _walk_json(value: Any):
    if isinstance(value, dict):
        for item in value.values():
            yield from _walk_json(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_json(item)
    else:
        yield value


def _is_image_path(value: str) -> bool:
    lower = value.lower()
    return lower.endswith((".png", ".jpg", ".jpeg", ".webp")) and ("/" in value or value.startswith("~"))


def _looks_like_media_json(raw_json: str) -> bool:
    lower = raw_json.lower()
    return any(token in lower for token in ("photo", "image", "document", "media", "webpage"))


def _media_type_from_raw_json(raw_json: str) -> str:
    lower = raw_json.lower()
    if "photo" in lower or "image" in lower:
        return "图片"
    if "webpage" in lower:
        return "链接卡片"
    if "document" in lower:
        return "文件"
    return "媒体"


def _extract_sender(block: str) -> str:
    match = _WX_SENDER_RE.search(block)
    return match.group("sender") if match else "unknown"


def _nearby_context(text: str, start: int, end: int, radius: int = 400) -> str:
    left = max(0, start - radius)
    right = min(len(text), end + radius)
    return text[left:right].strip()


def _telegram_context(rows: list[Any], idx: int, radius: int = 3) -> str:
    parts: list[str] = []
    for row in rows[max(0, idx - radius):idx + radius + 1]:
        sender = row["sender_name"] or "unknown"
        content = (row["content"] or "").strip()
        if content:
            parts.append(f"{sender}: {content}")
    return "\n".join(parts)
