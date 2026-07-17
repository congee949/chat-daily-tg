"""Telegram Bot API 10.2 rich-markdown presentation for a HealthReport."""
from __future__ import annotations

from chat_daily_tg.health_briefing import HealthReport, _progress


def _duration(hours: float | None) -> str:
    if hours is None:
        return "—"
    minutes = int(round(hours * 60))
    return f"{minutes // 60}小时{minutes % 60:02d}分"


def _number(value: float | None, unit: str, digits: int = 0) -> str:
    return "—" if value is None else f"{value:.{digits}f}{unit}"


def _signed_delta(
    value: float | None,
    baseline: float | None,
    unit: str,
    *,
    digits: int = 0,
) -> str:
    if value is None or baseline is None:
        return "—"
    text = f"{value - baseline:+.{digits}f}"
    if float(text) == 0:
        # exact-half deltas round-half-even to "+0"/"-0"; collapse to unsigned zero
        return f"{0:.{digits}f}{unit}"
    return f"{text}{unit}"


def _sleep_delta(value: float | None, baseline: float | None) -> str:
    if value is None or baseline is None:
        return "—"
    return _signed_delta((value - baseline) * 60, 0.0, "分钟")


def _summary(report: HealthReport) -> str:
    parts: list[str] = []
    sleep = report.sleep
    sleep_base = report.medians.get("sleep")
    if sleep and sleep_base:
        ratio = sleep.asleep_hours / sleep_base
        if 0.9 <= ratio <= 1.1:
            parts.append("最近完整睡眠时长接近个人近期水平")
        elif ratio < 0.9:
            parts.append("最近完整睡眠时长低于个人近期水平")
        else:
            parts.append("最近完整睡眠时长高于个人近期水平")
    exercise_base = report.medians.get("exercise")
    exercise = report.activity.exercise_min
    if exercise is not None and exercise_base:
        ratio = exercise / exercise_base
        if ratio >= 1.2:
            parts.append("昨日锻炼明显多于近期日常")
        elif ratio <= 0.8:
            parts.append("昨日锻炼少于近期日常")
        else:
            parts.append("昨日锻炼接近近期日常")
    if not parts:
        return "昨日健康数据已记录，详细数值见下方折叠内容。"
    return "；".join(parts) + "。"


def build_health_rich_markdown(report: HealthReport, *, chart_media_id: str | None) -> str:
    progress, bar = _progress(report.briefing_day)
    activity = report.activity
    sleep = report.sleep
    lines = [
        f"### 🌤️ 个人晨报 · {report.briefing_day.isoformat()}",
        "",
        f"{progress}  \n`{bar}`",
    ]
    if report.wake_sleep:
        lines.extend(["", f"起床：**{report.wake_sleep.end:%H:%M}**（依据最后睡眠阶段推定）"])
    else:
        lines.extend(["", "起床：今晨睡眠数据尚未同步，暂不判断"])
    if chart_media_id:
        lines.extend(["", f"![](tg://photo?id={chart_media_id})"])

    lines.extend([
        "",
        _summary(report),
        "",
        "<details><summary>查看睡眠、训练与精确数据</summary>",
        "",
    ])
    if sleep:
        lines.extend([
            "#### 😴 睡眠",
            "",
            "| 项目 | 数据 |",
            "|:---|---:|",
            f"| 记录口径 | {report.sleep_label} |",
            f"| 睡眠时段 | {sleep.start:%m-%d %H:%M}–{sleep.end:%m-%d %H:%M} |",
            f"| 实睡 | {_duration(sleep.asleep_hours)} |",
            f"| 核心睡眠 | {_duration(sleep.core_hours)} |",
            f"| 深度睡眠 | {_duration(sleep.deep_hours)} |",
            f"| REM 睡眠 | {_duration(sleep.rem_hours)} |",
            f"| 清醒 | {_duration(sleep.awake_hours)} |",
            "",
        ])
    lines.extend([
        "#### 🏋️ 训练",
        "",
        "| 项目 | 消耗能量 |",
        "|:---|---:|",
    ])
    if report.workouts:
        for workout in report.workouts:
            lines.append(
                f"| {workout.name} | {_number(workout.active_kcal, '千卡')} |"
            )
    else:
        lines.append("| 未记录 Apple Watch 体能训练 | — |")
    lines.extend([
        "",
        "#### 📊 相对近期差值",
        "",
        "| 指标 | 差值 |",
        "|:---|---:|",
        f"| 睡眠 | {_sleep_delta(sleep.asleep_hours if sleep else None, report.medians.get('sleep'))} |",
        f"| 锻炼 | {_signed_delta(activity.exercise_min, report.medians.get('exercise'), '分钟')} |",
        f"| 移动距离 | {_signed_delta(activity.distance_km, report.medians.get('distance'), '公里', digits=2)} |",
        f"| 站立 | {_signed_delta(activity.stand_hours, report.medians.get('stand'), '小时')} |",
    ])
    missing = [
        key for key in ("sleep", "exercise", "distance", "stand")
        if report.medians.get(key) is None
    ]
    if missing:
        samples = min(report.baseline_samples.get(key, 0) for key in missing)
        lines.extend([
            "",
            f"近期基线样本不足（{samples} 天，需 {report.min_baseline_samples} 天），缺基线的行暂不比较",
        ])
    lines.extend([
        "",
        "</details>",
        "",
        "---",
    ])
    return "\n".join(lines)
