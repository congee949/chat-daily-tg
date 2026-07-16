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


def _baseline(report: HealthReport, key: str, value: str) -> str:
    count = report.baseline_samples.get(key, 0)
    if report.medians.get(key) is None:
        return f"样本 {count} 天，暂不比较"
    return value


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
        lines.extend(["", f'![](tg://photo?id={chart_media_id} "昨日睡眠与训练概览")'])

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
        "| 时段 | 项目 | 时长 | 距离 | 活动能量 |",
        "|:---|:---|---:|---:|---:|",
    ])
    if report.workouts:
        for workout in report.workouts:
            timing = (
                f"{workout.start:%H:%M}–{workout.end:%H:%M}"
                if workout.start and workout.end else "—"
            )
            lines.append(
                f"| {timing} | {workout.name} | {workout.duration_min:.0f}分钟 | "
                f"{_number(workout.distance_km, '公里', 2)} | "
                f"{_number(workout.active_kcal, '千卡')} |"
            )
    else:
        lines.append("| — | 未记录 Apple Watch 体能训练 | — | — | — |")
    lines.extend([
        "",
        "#### 📊 活动与近期数据",
        "",
        "| 指标 | 昨日 | 近期值 |",
        "|:---|---:|---:|",
        f"| 睡眠 | {_duration(sleep.asleep_hours if sleep else None)} | "
        f"{_baseline(report, 'sleep', _duration(report.medians.get('sleep')))} |",
        f"| 锻炼 | {_number(activity.exercise_min, '分钟')} | "
        f"{_baseline(report, 'exercise', _number(report.medians.get('exercise'), '分钟'))} |",
        f"| 移动距离 | {_number(activity.distance_km, '公里', 2)} | "
        f"{_baseline(report, 'distance', _number(report.medians.get('distance'), '公里', 2))} |",
        f"| 站立 | {_number(activity.stand_hours, '小时')} | "
        f"{_baseline(report, 'stand', _number(report.medians.get('stand'), '小时'))} |",
        "",
        "</details>",
        "",
        "---",
    ])
    return "\n".join(lines)
