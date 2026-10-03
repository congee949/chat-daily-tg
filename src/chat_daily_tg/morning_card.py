"""Morning card: yesterday's energy and last night's sleep as one Telegram photo.

The panel is built once from the health report and the energy ledger; the PNG
and the HTML caption are both rendered from it, so they cannot disagree.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
import html
import logging
import re
from pathlib import Path

from PIL import Image, ImageDraw

from chat_daily_tg.health_briefing import HealthReport, SleepEpisode
from chat_daily_tg.health_card import _font

log = logging.getLogger(__name__)

WIDTH, HEIGHT = 1080, 1350  # 4:5, shown uncropped in Telegram's mobile chat view
BG = "#f4f5f7"
PANEL = "#ffffff"
BORDER = "#e3e6eb"
TEXT = "#1d2330"
MUTED = "#6b7484"
BAR = "#d5d9e0"
ENERGY = "#e8772e"
SLEEP = "#2f7df6"
CORE = "#5b9bff"
DEEP = "#8a6cf0"
REM = "#2fb37a"
AWAKE = "#ef6b6b"
WEEKDAYS = "一二三四五六日"
CAPTION_LIMIT = 1000  # Telegram allows 1024 visible characters


@dataclass(frozen=True)
class MorningPanel:
    report_day: date
    energy_kwh: float | None = None
    energy_series: list[tuple[date, float | None]] = field(default_factory=list)
    energy_baseline: float | None = None
    energy_baseline_days: int = 0
    energy_periods: list[str] = field(default_factory=list)
    sleep: SleepEpisode | None = None
    health: HealthReport | None = None

    @property
    def briefing_day(self) -> date:
        return self.report_day + timedelta(days=1)


def build_panel(
    report_day: date,
    health: HealthReport | None,
    energy_records: dict | None,
) -> MorningPanel:
    energy: dict = {}
    if energy_records is not None:
        from chat_daily_tg.energy_usage import energy_series, period_summaries, prior_mean

        current = energy_records.get(report_day.isoformat())
        baseline, days = prior_mean(energy_records, report_day)
        energy = {
            "energy_kwh": current.kwh if current else None,
            "energy_series": energy_series(energy_records, report_day),
            "energy_baseline": baseline,
            "energy_baseline_days": days,
            "energy_periods": period_summaries(energy_records, report_day),
        }
    return MorningPanel(
        report_day=report_day,
        sleep=health.last_night_sleep if health else None,
        health=health,
        **energy,
    )


def _duration(hours: float) -> str:
    minutes = round(hours * 60)
    return f"{minutes // 60}小时{minutes % 60:02d}分"


def _energy_delta(panel: MorningPanel) -> str | None:
    if panel.energy_kwh is None or panel.energy_baseline is None:
        return None
    change = panel.energy_kwh - panel.energy_baseline
    if abs(change) < 0.005:
        return "与前 7 日均值持平"
    return f"比前 7 日均值{'高' if change > 0 else '低'} {abs(change):.2f}"


class _Canvas:
    def __init__(self) -> None:
        self.image = Image.new("RGB", (WIDTH, HEIGHT), BG)
        self.draw = ImageDraw.Draw(self.image)

    def width(self, text: str, size: int) -> float:
        return self.draw.textbbox((0, 0), text, font=_font(size))[2]

    def text(self, xy, text: str, size: int, fill: str) -> None:
        self.draw.text(xy, text, font=_font(size), fill=fill)

    def panel(self, top: int, bottom: int) -> None:
        self.draw.rounded_rectangle((40, top, WIDTH - 40, bottom), radius=32,
                                    fill=PANEL, outline=BORDER, width=2)


def _energy_section(c: _Canvas, panel: MorningPanel, top: int) -> None:
    c.panel(top, top + 580)
    c.text((80, top + 30), "昨日用电", 34, MUTED)
    if panel.energy_kwh is None:
        c.text((80, top + 80), "暂缺", 96, MUTED)
        c.text((80, top + 230), "Home Assistant 没有昨日收盘读数", 32, MUTED)
    else:
        big = f"{panel.energy_kwh:.2f}"
        c.text((80, top + 80), big, 120, ENERGY)
        c.text((92 + c.width(big, 120), top + 150), "kWh", 40, MUTED)
        delta = _energy_delta(panel)
        note = delta or f"历史不足 3 天（{panel.energy_baseline_days}/7），暂不比较"
        c.text((80, top + 230), note, 32, TEXT if delta else MUTED)

    series = panel.energy_series
    if not series:
        return
    base, peak_y = top + 510, top + 310
    values = [v for _, v in series if v is not None]
    vmax = max(values + [panel.energy_baseline or 0, 0.1]) * 1.1
    slot = (WIDTH - 160) / len(series)
    bar_w = slot * 0.6
    if panel.energy_baseline is not None:
        y = base - (base - peak_y) * panel.energy_baseline / vmax
        for x in range(80, WIDTH - 80, 26):
            c.draw.line((x, y, min(x + 14, WIDTH - 80), y), fill=MUTED, width=3)
    for i, (day, value) in enumerate(series):
        cx = 80 + slot * i + slot / 2
        today = day == panel.report_day
        if value is None:
            c.draw.rounded_rectangle((cx - bar_w / 2, base - 24, cx + bar_w / 2, base),
                                     radius=8, outline=BAR, width=3)
        else:
            h = max((base - peak_y) * value / vmax, 6)
            c.draw.rounded_rectangle((cx - bar_w / 2, base - h, cx + bar_w / 2, base),
                                     radius=10, fill=ENERGY if today else BAR)
        label = "昨天" if today else f"{day.day}日"
        c.text((cx - c.width(label, 26) / 2, base + 12), label, 26,
               ENERGY if today else MUTED)


def _sleep_section(c: _Canvas, panel: MorningPanel, top: int) -> None:
    c.panel(top, HEIGHT - 50)
    c.text((80, top + 30), "昨夜实睡", 34, MUTED)
    sleep = panel.sleep
    if sleep is None:
        c.text((80, top + 80), "待补", 96, MUTED)
        c.text((80, top + 210), "睡眠尚未同步，同步后更新这张卡片", 32, MUTED)
        return
    c.text((80, top + 80), _duration(sleep.asleep_hours), 96, SLEEP)
    c.text((80, top + 210), f"{sleep.start:%H:%M} 入睡  →  {sleep.end:%H:%M} 起床", 32, TEXT)
    stages = [("核心", sleep.core_hours, CORE), ("深睡", sleep.deep_hours, DEEP),
              ("REM", sleep.rem_hours, REM), ("清醒", sleep.awake_hours, AWAKE)]
    total = sum(hours for _, hours, _ in stages)
    if total <= 0:
        return
    x, right, bar_top = 80.0, WIDTH - 80, top + 280
    for i, (_, hours, color) in enumerate(stages):
        seg = (right - 80) * hours / total
        if seg >= 1:
            c.draw.rounded_rectangle((x, bar_top, x + seg - (5 if i < 3 else 0), bar_top + 70),
                                     radius=12, fill=color)
        x += seg
    for i, (label, hours, color) in enumerate(stages):
        lx, ly = 80 + (i % 2) * 470, bar_top + 105 + (i // 2) * 70
        c.draw.rounded_rectangle((lx, ly + 8, lx + 26, ly + 34), radius=6, fill=color)
        value = f"{hours * 60:.0f} 分" if label == "清醒" else f"{hours:.1f} 小时"
        c.text((lx + 42, ly), f"{label}  {value}", 32, TEXT)


def render_morning_card(panel: MorningPanel, out_path: Path) -> Path | None:
    """Render the card; any failure returns None so delivery can fall back to text."""
    try:
        c = _Canvas()
        day = panel.briefing_day
        c.text((60, 50), f"{day.month}月{day.day}日 周{WEEKDAYS[day.weekday()]}", 34, MUTED)
        _energy_section(c, panel, 120)
        _sleep_section(c, panel, 730)
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        c.image.save(out_path, format="PNG", optimize=True)
        return out_path
    except Exception as exc:
        log.warning("morning card render failed: %s", exc)
        return None


def _detail_lines(panel: MorningPanel) -> list[str]:
    lines: list[str] = []
    if panel.energy_series:
        if panel.energy_baseline is not None:
            lines.append(f"用电：前 7 日均值 {panel.energy_baseline:.2f} kWh"
                         f"（{panel.energy_baseline_days}/7 天）")
        lines.extend(f"用电：{line}" for line in panel.energy_periods)
    sleep = panel.sleep
    if sleep:
        lines.append(f"睡眠：{sleep.start:%H:%M}–{sleep.end:%H:%M}，实睡 {_duration(sleep.asleep_hours)}")
        lines.append(f"核心 {sleep.core_hours:.1f}h · 深睡 {sleep.deep_hours:.1f}h · "
                     f"REM {sleep.rem_hours:.1f}h · 清醒 {sleep.awake_hours * 60:.0f} 分")
    if panel.health:
        a = panel.health.activity
        activity = []
        if a.active_kcal is not None and a.active_kcal >= 1:
            activity.append(f"活动 {a.active_kcal:.0f} kcal")
        if a.exercise_min is not None:
            activity.append(f"锻炼 {a.exercise_min:.0f} 分钟")
        if a.steps is not None:
            activity.append(f"{a.steps:.0f} 步")
        if activity:
            lines.append("活动：" + " · ".join(activity))
        recovery = []
        if a.resting_hr:
            recovery.append(f"静息心率 {a.resting_hr:.0f} bpm")
        if a.hrv_ms:
            recovery.append(f"HRV {a.hrv_ms:.0f} ms")
        if recovery:
            lines.append("恢复：" + "，".join(recovery))
    return lines


def morning_caption(panel: MorningPanel) -> str:
    """Telegram HTML caption: two summary lines plus an expandable detail quote."""
    day = panel.briefing_day
    head = [f"<b>晨报 · {day.month}月{day.day}日 周{WEEKDAYS[day.weekday()]}</b>", ""]
    if panel.energy_series:
        if panel.energy_kwh is None:
            head.append("用电数据暂缺；")
        else:
            delta = _energy_delta(panel)
            head.append(f"用电 {panel.energy_kwh:.2f} kWh" + (f"，{delta}；" if delta else "；"))
    sleep = panel.sleep
    if sleep:
        head.append(f"实睡 {_duration(sleep.asleep_hours)}，起床 {sleep.end:%H:%M}。")
    elif panel.health is not None:
        head.append("昨夜睡眠尚未同步，同步后更新本条。")
    details = _detail_lines(panel)
    while details:
        quote = "<blockquote expandable>" + html.escape("\n".join(details)) + "</blockquote>"
        caption = "\n".join(head) + "\n\n" + quote
        if len(html.unescape(_strip_tags(caption))) <= CAPTION_LIMIT:
            return caption
        details.pop()
    return "\n".join(head)


def _strip_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text)
