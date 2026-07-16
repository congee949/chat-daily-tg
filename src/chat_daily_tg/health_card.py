"""Render a compact, baseline-aware Health/Apple Watch overview PNG."""
from __future__ import annotations

import logging
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from chat_daily_tg.health_briefing import HealthReport

log = logging.getLogger(__name__)

WIDTH = 1200
HEIGHT = 1080
BG = "#0f1115"
PANEL = "#181c23"
TEXT = "#f4f6f8"
MUTED = "#929baa"
GRID = "#2b313c"
BLUE = "#63b3ff"
GREEN = "#6cdaa0"
PURPLE = "#b9a0ff"
ORANGE = "#ffbd6a"
RED = "#ff8585"

_FONT_CANDIDATES = (
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Light.ttc",
    "/System/Library/Fonts/Helvetica.ttc",
)


def _font(size: int, index: int = 0) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size=size, index=index)
        except OSError:
            continue
    return ImageFont.load_default()


def _ratio(value: float | None, baseline: float | None) -> float | None:
    if value is None or baseline is None or baseline <= 0:
        return None
    return value / baseline


def _relative_symbol(ratio: float | None) -> tuple[str, str]:
    if ratio is None:
        return "–", MUTED
    if ratio >= 1.2:
        return "↑", GREEN
    if ratio <= 0.8:
        return "↓", RED
    return "=", MUTED


def _metric_row(
    draw: ImageDraw.ImageDraw,
    y: int,
    label: str,
    ratio: float | None,
) -> None:
    draw.text((82, y), label, font=_font(28), fill=MUTED)
    x0, y0, x1 = 310, y + 9, 1015
    draw.rounded_rectangle((x0, y0, x1, y0 + 18), radius=9, fill=GRID)
    if ratio is not None:
        fill = max(0.03, min(ratio / 1.5, 1.0))
        color = GREEN if 0.85 <= ratio <= 1.15 else (ORANGE if ratio < 0.85 else BLUE)
        draw.rounded_rectangle((x0, y0, x0 + int((x1 - x0) * fill), y0 + 18),
                               radius=9, fill=color)
        baseline_x = x0 + int((x1 - x0) / 1.5)
        draw.line((baseline_x, y0 - 5, baseline_x, y0 + 23), fill=TEXT, width=3)
    symbol, symbol_color = _relative_symbol(ratio)
    draw.text((1060, y - 10), symbol, font=_font(46), fill=symbol_color)
    draw.text((310, y + 36), "白线 = 近期中位数", font=_font(21), fill=MUTED)


def _sleep_timeline(draw: ImageDraw.ImageDraw, report: HealthReport, y: int) -> int:
    sleep = report.sleep
    draw.text((66, y), "睡眠构成", font=_font(31), fill=TEXT)
    if sleep is None:
        draw.text((66, y + 55), "尚无完整睡眠记录", font=_font(27), fill=MUTED)
        return y + 115
    total = max(
        sleep.core_hours + sleep.deep_hours + sleep.rem_hours + sleep.awake_hours,
        0.01,
    )
    x, bar_y, width = 66, y + 62, 1068
    stages = (
        ("核心", sleep.core_hours, BLUE),
        ("深睡", sleep.deep_hours, PURPLE),
        ("REM", sleep.rem_hours, GREEN),
        ("清醒", sleep.awake_hours, RED),
    )
    for label, hours, color in stages:
        segment = int(width * hours / total)
        if segment > 0:
            draw.rectangle((x, bar_y, x + segment, bar_y + 38), fill=color)
            x += segment
    legend_x = 66
    for label, hours, color in stages:
        draw.rounded_rectangle((legend_x, bar_y + 58, legend_x + 18, bar_y + 76),
                               radius=4, fill=color)
        text = label
        draw.text((legend_x + 28, bar_y + 52), text, font=_font(22), fill=MUTED)
        legend_x += draw.textbbox((0, 0), text, font=_font(22))[2] + 72
    return bar_y + 105


def _workout_timeline(draw: ImageDraw.ImageDraw, report: HealthReport, y: int) -> int:
    draw.text((66, y), "昨日训练", font=_font(31), fill=TEXT)
    if not report.workouts:
        draw.text((66, y + 55), "未记录 Apple Watch 体能训练", font=_font(27), fill=MUTED)
        return y + 115
    x, bar_y, width = 66, y + 62, 1068
    durations = [max(w.duration_min, 0) for w in report.workouts]
    gaps: list[float] = []
    for current, following in zip(report.workouts, report.workouts[1:]):
        if current.end and following.start:
            gaps.append(max((following.start - current.end).total_seconds() / 60, 0))
        else:
            gaps.append(0)
    total = max(sum(durations) + sum(gaps), 1)
    colors = (BLUE, PURPLE, GREEN, ORANGE)
    cursor = x
    for i, workout in enumerate(report.workouts):
        segment = max(8, int(width * durations[i] / total))
        draw.rounded_rectangle((cursor, bar_y, cursor + segment, bar_y + 38),
                               radius=8, fill=colors[i % len(colors)])
        cursor += segment
        if i < len(gaps):
            cursor += int(width * gaps[i] / total)
    label = "  →  ".join(w.name for w in report.workouts)
    draw.text((66, bar_y + 58), label, font=_font(22), fill=MUTED)
    return bar_y + 112


def render_health_card(report: HealthReport, out_path: Path) -> Path | None:
    """Render one deterministic PNG. Missing metrics are displayed as unknown."""
    try:
        image = Image.new("RGB", (WIDTH, HEIGHT), BG)
        draw = ImageDraw.Draw(image)
        draw.text((66, 52), "个人健康日报", font=_font(50), fill=TEXT)
        date_text = report.report_day.isoformat()
        date_width = draw.textbbox((0, 0), date_text, font=_font(28))[2]
        draw.text((1134 - date_width, 72), date_text, font=_font(28), fill=MUTED)
        draw.line((66, 132, 1134, 132), fill=GRID, width=2)

        draw.rounded_rectangle((46, 165, 1154, 522), radius=26, fill=PANEL)
        draw.text((76, 193), "昨日状态 · 相对个人近期中位数", font=_font(25), fill=MUTED)
        sleep = report.sleep
        _metric_row(
            draw, 252, "睡眠",
            _ratio(sleep.asleep_hours if sleep else None, report.medians.get("sleep")),
        )
        _metric_row(
            draw, 342, "锻炼",
            _ratio(report.activity.exercise_min, report.medians.get("exercise")),
        )
        _metric_row(
            draw, 432, "距离",
            _ratio(report.activity.distance_km, report.medians.get("distance")),
        )

        y = _sleep_timeline(draw, report, 558)
        y = _workout_timeline(draw, report, y + 18)
        footer = report.sleep_label
        draw.text((66, min(y + 20, 1038)), footer, font=_font(20), fill=MUTED)

        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        image.save(out_path, format="PNG", optimize=True)
        return out_path
    except Exception as exc:
        log.warning("health card render failed: %s", exc)
        return None
