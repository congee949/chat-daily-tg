from __future__ import annotations
from collections import defaultdict
from pathlib import Path
from wx_daily_tg.db import PermanentDB


CATEGORY_LABELS = {
    "invite_code": "邀请码 / 推荐码",
    "bank_product": "银行 / 金融产品",
    "activity": "活动 / 优惠",
    "misc": "其他",
}


def regenerate_permanent_md(db_path: Path, md_path: Path) -> None:
    db = PermanentDB(db_path)
    by_cat: dict[str, list] = defaultdict(list)
    for e in db.read_all():
        by_cat[e.category].append(e)

    lines = ["# 永久机会库", "", "> 此文件由脚本自动生成，不要手动编辑。改 `permanent.jsonl`。", ""]
    for cat, label in CATEGORY_LABELS.items():
        entries = sorted(by_cat.get(cat, []), key=lambda e: e.captured_at, reverse=True)
        if not entries:
            continue
        lines.append(f"## {label}")
        lines.append("")
        lines.append("| 状态 | 标题 | 内容 | 来源 | 抓取时间 | ID |")
        lines.append("|---|---|---|---|---|---|")
        for e in entries:
            status_icon = {"alive": "✅", "likely_dead": "⚠️", "dead": "💀", "unknown": "❓"}.get(e.status, "?")
            row = (
                f"| {status_icon} {e.status} | {e.title} | {e.content} "
                f"| {e.source_group} / {e.source_sender} | {e.captured_at} | `{e.id}` |"
            )
            lines.append(row)
        lines.append("")

    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text("\n".join(lines), encoding="utf-8")
