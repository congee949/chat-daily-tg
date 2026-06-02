"""Render the daily concise summary into a PNG "card" for Telegram sendPhoto.

Deterministic and dependency-free: parses the fixed `### emoji 标题` schema the LLM
emits (see prompts.py) into sections, fills an f-string HTML template, and screenshots
it with the locally-installed Google Chrome in headless mode. No Jinja2, no Playwright,
no LLM/agent calls — safe to run unattended under launchd.

The image is an *add-on*: callers send it before the full text message and fall back to
text-only on any failure (render returns None / send_photo raises).
"""
from __future__ import annotations

import html
import logging
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

# Heading keyword -> CardData field. Matched by substring against the `### ` heading
# text, so it tolerates the emoji prefix and minor wording changes. Order = render order.
_SECTION_MAP: list[tuple[tuple[str, ...], str]] = [
    (("总览",), "overview"),
    (("钱", "活动"), "money"),
    (("AI", "工具"), "ai_tools"),
    (("风险", "待验证"), "risks"),
    (("重复", "旧闻"), "repeats"),
    (("资源",), "resources"),
]
# Sections that should NOT appear on the card (local file paths etc.).
_DROP_KEYWORDS = ("详情",)

_SECTION_TITLES = {
    "overview": "🌅 今日总览",
    "money": "💰 钱 / 活动",
    "ai_tools": "🧠 AI / 工具",
    "risks": "⚠️ 风险 / 待验证",
    "repeats": "🔁 重复 / 旧闻",
    "resources": "🔗 资源",
}

# Candidate Chrome binaries, primary path first.
_CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
]


@dataclass
class CardData:
    date: str
    overview: list[str] = field(default_factory=list)
    money: list[str] = field(default_factory=list)
    ai_tools: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    repeats: list[str] = field(default_factory=list)
    resources: list[str] = field(default_factory=list)

    @property
    def risk_count(self) -> int:
        return len(self.risks)

    def sections(self) -> list[tuple[str, list[str]]]:
        """Non-empty (field_key, items) pairs in render order."""
        out = []
        for _keys, fieldname in _SECTION_MAP:
            items = getattr(self, fieldname)
            if items:
                out.append((fieldname, items))
        return out


def _clean_item(line: str) -> str:
    """Strip a leading bullet and **bold** markers; keep the rest (incl. source tails)."""
    line = line.strip()
    if line.startswith("- "):
        line = line[2:].strip()
    line = line.replace("**", "")
    return line


def parse_concise_to_card(concise_md: str, date_str: str) -> CardData:
    """Bucket the concise markdown into CardData by its fixed `### ` heading schema."""
    card = CardData(date=date_str)
    current: str | None = None
    for raw in concise_md.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        if line.lstrip().startswith("#"):
            heading = line.lstrip("#").strip()
            current = None
            if any(k in heading for k in _DROP_KEYWORDS):
                continue
            for keys, fieldname in _SECTION_MAP:
                if any(k in heading for k in keys):
                    current = fieldname
                    break
            continue
        if current is None:
            continue
        item = _clean_item(line)
        if item:
            getattr(card, current).append(item)
    return card


def card_caption(card: CardData) -> str:
    """A short plain-text teaser for the photo caption (<=1024, well under to be safe)."""
    head = f"📨 每日群聊速览 · {card.date}"
    lead = card.overview[0] if card.overview else ""
    cap = f"{head}\n{lead}".strip()
    if len(cap) > 900:
        cap = cap[:897] + "…"
    return cap


_CSS = """
* { margin: 0; padding: 0; box-sizing: border-box; }
html, body { background: #0f1115; }
body {
  width: 760px;
  font-family: 'PingFang SC','Hiragino Sans GB','STHeiti','Microsoft YaHei',sans-serif;
  color: #e6e8eb; padding: 36px 40px 40px;
}
.head { display: flex; align-items: baseline; justify-content: space-between;
  border-bottom: 2px solid #2a2f3a; padding-bottom: 16px; margin-bottom: 8px; }
.title { font-size: 30px; font-weight: 700; color: #fff; }
.date { font-size: 18px; color: #8a93a3; }
.section { margin-top: 22px; }
.sec-title { font-size: 20px; font-weight: 700; margin-bottom: 10px; }
.item { font-size: 19px; line-height: 1.55; color: #d7dbe0;
  padding: 5px 0 5px 18px; position: relative; }
.item::before { content: "•"; position: absolute; left: 0; color: #5b6573; }
.s-money .sec-title { color: #ffd479; }
.s-ai_tools .sec-title { color: #7bd0ff; }
.s-risks .sec-title { color: #ff8f8f; }
.s-repeats .sec-title { color: #b9a4ff; }
.s-resources .sec-title { color: #84e0a8; }
.s-overview .sec-title { color: #fff; }
.foot { margin-top: 28px; padding-top: 16px; border-top: 1px solid #2a2f3a;
  font-size: 16px; color: #8a93a3; display: flex; justify-content: space-between; }
.badge { color: #ff8f8f; font-weight: 600; }
"""


def _build_html(card: CardData) -> str:
    blocks: list[str] = []
    for fieldname, items in card.sections():
        title = html.escape(_SECTION_TITLES.get(fieldname, fieldname))
        rows = "\n".join(f'<div class="item">{html.escape(it)}</div>' for it in items)
        blocks.append(
            f'<div class="section s-{fieldname}">'
            f'<div class="sec-title">{title}</div>{rows}</div>'
        )
    body = "\n".join(blocks)
    badge = (
        f'<span class="badge">⚠️ {card.risk_count} 项待验证</span>'
        if card.risk_count else "<span></span>"
    )
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<style>{_CSS}</style></head><body>"
        f'<div class="head"><span class="title">每日群聊速览</span>'
        f'<span class="date">{html.escape(card.date)}</span></div>'
        f"{body}"
        f'<div class="foot">{badge}<span>chat-daily-tg</span></div>'
        "</body></html>"
    )


def _find_chrome() -> str | None:
    for cand in _CHROME_CANDIDATES:
        if "/" in cand:
            if Path(cand).exists():
                return cand
        else:
            found = shutil.which(cand)
            if found:
                return found
    return None


def _estimate_height(card: CardData) -> int:
    # header + per-section(title + items) + footer; clamp so we neither crop nor
    # leave a huge dark gap. Extra space blends into the dark background.
    h = 140
    for _fieldname, items in card.sections():
        h += 52 + len(items) * 36
    h += 80
    return max(420, min(h, 4000))


def render_card_png(card: CardData, out_path: Path) -> Path | None:
    """Render the card to a PNG via headless Chrome. Returns None on any failure."""
    if not card.sections():
        log.info("card render skipped: no renderable sections")
        return None
    chrome = _find_chrome()
    if not chrome:
        log.warning("card render skipped: no Chrome/Chromium binary found")
        return None
    try:
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        height = _estimate_height(card)
        with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False, encoding="utf-8") as fh:
            fh.write(_build_html(card))
            html_path = fh.name
        cmd = [
            chrome, "--headless=new", "--disable-gpu", "--no-sandbox",
            "--hide-scrollbars", "--force-device-scale-factor=2",
            f"--window-size=760,{height}",
            f"--screenshot={out_path}", f"file://{html_path}",
        ]
        proc = subprocess.run(cmd, capture_output=True, timeout=60)
        Path(html_path).unlink(missing_ok=True)
        if proc.returncode != 0 or not out_path.exists() or out_path.stat().st_size == 0:
            log.warning("card render failed (rc=%s): %s", proc.returncode,
                        proc.stderr.decode("utf-8", "replace")[:300])
            return None
        return out_path
    except Exception as e:
        log.warning("card render error: %s", e)
        return None
