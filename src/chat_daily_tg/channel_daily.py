from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import sqlite3

from chat_daily_tg.archive import safe_filename
from chat_daily_tg.telegram_exporter import (
    ExportResult,
    canonical_chat_ids,
    export_chat,
    parse_timestamp,
    read_messages,
    render_message,
)


@dataclass(frozen=True)
class ChannelConfig:
    id: str
    name: str
    section: str
    limit: int = 500


@dataclass(frozen=True)
class SourceItem:
    ref: str
    channel: str
    msg_id: int
    timestamp: str
    text: str
    sender: str = "unknown"
    url: str | None = None


@dataclass(frozen=True)
class ChannelExport:
    config: ChannelConfig
    content: str
    message_count: int
    skipped_count: int
    sources: list[SourceItem]


@dataclass(frozen=True)
class ChannelDigestResult:
    date_str: str
    raw_markdown: str
    summary_markdown: str
    exports: list[ChannelExport]
    usage: dict


DEFAULT_CHANNELS = [
    ChannelConfig(id="-1002631914757", name="投机之路", section="市场 / 投机"),
    ChannelConfig(id="-1001125107539", name="科技圈🎗在花频道📮", section="科技 / AI / 工具"),
    ChannelConfig(id="-1003471026810", name="LydiaPod", section="身心灵 / 健康"),
    ChannelConfig(id="-1003452282759", name="Gary Playa", section="产品 / 观点"),
]


SYSTEM_PROMPT = "你是信息筛选助手。你的任务是把昨天 Telegram 频道内容压缩成一条手机上容易读的中文简报。只依据提供的原文，不要编造原文没有的信息。"


def collect_channel_exports(
    *,
    date_str: str,
    next_day: str,
    archive_dir: Path,
    db_path: str,
    sync_before_export: bool,
    channels: list[ChannelConfig] | None = None,
) -> list[ChannelExport]:
    out: list[ChannelExport] = []
    for idx, channel in enumerate(channels or DEFAULT_CHANNELS, start=1):
        result = export_chat(
            chat_id=channel.id,
            chat_name=channel.name,
            since=date_str,
            until=next_day,
            out_path=archive_dir / f"telegram-channel-{safe_filename(channel.name)}.md",
            db_path=db_path,
            limit=channel.limit,
            sync_before_export=sync_before_export,
        )
        if result.content.strip() and result.message_count > 0:
            out.append(_from_export_result(channel, result, channel_index=idx, date_str=date_str, next_day=next_day, db_path=db_path))
    return out


def _from_export_result(
    channel: ChannelConfig,
    result: ExportResult,
    *,
    channel_index: int = 1,
    date_str: str | None = None,
    next_day: str | None = None,
    db_path: str | Path | None = None,
) -> ChannelExport:
    sources = _source_items(
        channel=channel,
        channel_index=channel_index,
        date_str=date_str,
        next_day=next_day,
        db_path=db_path,
        fallback_content=result.content,
    )
    content = _content_with_refs(result.content, sources)
    return ChannelExport(
        config=channel,
        content=content,
        message_count=result.message_count,
        skipped_count=result.skipped_count,
        sources=sources,
    )


def _source_items(
    *,
    channel: ChannelConfig,
    channel_index: int,
    date_str: str | None,
    next_day: str | None,
    db_path: str | Path | None,
    fallback_content: str,
) -> list[SourceItem]:
    if date_str and next_day and db_path:
        try:
            rows = read_messages(
                db_path=Path(db_path).expanduser(),
                chat_id=channel.id,
                since=date_str,
                until=next_day,
                limit=channel.limit,
            )
            out: list[SourceItem] = []
            for row in rows:
                rendered = render_message(row, fallback_chat_name=channel.name)
                if rendered is None:
                    continue
                ref = f"C{channel_index:02d}-{len(out) + 1:03d}"
                out.append(_source_from_row(ref, channel, row, rendered))
            return out
        except (sqlite3.Error, OSError, ValueError):
            pass
    return _fallback_sources(channel=channel, channel_index=channel_index, content=fallback_content)


def _source_from_row(ref: str, channel: ChannelConfig, row: sqlite3.Row, rendered: str) -> SourceItem:
    msg_id = int(row["msg_id"])
    return SourceItem(
        ref=ref,
        channel=channel.name,
        msg_id=msg_id,
        timestamp=str(row["timestamp"]),
        text=rendered,
        sender=row["sender_name"] or "unknown",
        url=_message_url(channel.id, msg_id),
    )


def _fallback_sources(*, channel: ChannelConfig, channel_index: int, content: str) -> list[SourceItem]:
    out: list[SourceItem] = []
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith(">"):
            continue
        out.append(SourceItem(
            ref=f"C{channel_index:02d}-{len(out) + 1:03d}",
            channel=channel.name,
            msg_id=0,
            timestamp="",
            text=line,
        ))
    return out


def _message_url(chat_id: str, msg_id: int) -> str | None:
    ids = canonical_chat_ids(chat_id)
    positive = next((i for i in ids if i > 0 and str(i).startswith("100")), None)
    if positive is None:
        return None
    internal_id = str(positive)[3:]
    return f"https://t.me/c/{internal_id}/{msg_id}"


def _content_with_refs(content: str, sources: list[SourceItem]) -> str:
    if not sources:
        return content
    lines = content.splitlines()
    source_iter = iter(sources)
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[Telegram / "):
            try:
                item = next(source_iter)
            except StopIteration:
                out.append(line)
            else:
                out.append(f"[{item.ref}] {line}")
        else:
            out.append(line)
    return "\n".join(out).rstrip() + "\n"


def build_raw_markdown(date_str: str, exports: list[ChannelExport]) -> str:
    lines = [f"# 昨日频道原文｜{date_str}", ""]
    for item in exports:
        lines.extend([
            f"## {item.config.section}｜{item.config.name}",
            "",
            f"> 保留 {item.message_count} 条，跳过 {item.skipped_count} 条",
            "",
            item.content.strip(),
            "",
        ])
    return "\n".join(lines).rstrip() + "\n"


def build_channel_prompt(date_str: str, exports: list[ChannelExport]) -> str:
    if not exports:
        raise ValueError("no channel content exported")
    raw = build_raw_markdown(date_str, exports)
    return f"""请生成一条 Telegram 消息，标题必须是：📡 昨日频道推送｜{date_str}

要求：
- 按这些板块输出：市场 / 投机、科技 / AI / 工具、身心灵 / 健康、产品 / 观点、值得回看。
- 每个板块最多 5 条。
- 每条用一句话说明重点，不要照抄长段原文。
- 每条结论末尾必须带来源编号，例如：[C01-003]。多个来源写成：[C01-003][C02-011]。
- 没有内容的板块写“无高价值内容”。
- “值得回看”只放最值得打开原文再看的 3-6 条。
- 保留具体实体、产品名、资产名、人物名、事件名。
- 不要编造原文没有的信息。
- 不要输出寒暄、免责声明、结尾客套。

原文如下：

{raw}
"""


def validate_summary(text: str) -> str:
    cleaned = text.strip()
    if len(cleaned) < 20:
        raise ValueError("channel summary too short")
    return cleaned


def format_for_telegram(date_str: str, summary: str) -> str:
    cleaned = validate_summary(summary)
    title = f"📡 昨日频道推送｜{date_str}"
    if cleaned.startswith(title):
        return cleaned
    return f"{title}\n\n{cleaned}"


def write_channel_archive(archive_dir: Path, result: ChannelDigestResult) -> dict[str, Path]:
    archive_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "raw": archive_dir / "channel-daily-raw.md",
        "summary": archive_dir / "channel-daily-summary.md",
        "metadata": archive_dir / "channel-daily-metadata.json",
    }
    paths["raw"].write_text(_ensure_trailing_newline(result.raw_markdown), encoding="utf-8")
    paths["summary"].write_text(_ensure_trailing_newline(result.summary_markdown), encoding="utf-8")
    metadata = {
        "date": result.date_str,
        "usage": result.usage,
        "channels": [
            {
                "id": item.config.id,
                "name": item.config.name,
                "section": item.config.section,
                "message_count": item.message_count,
                "skipped_count": item.skipped_count,
            }
            for item in result.exports
        ],
        "sources": [
            _source_to_dict(source)
            for item in result.exports
            for source in item.sources
        ],
    }
    paths["metadata"].write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    return paths


def _source_to_dict(source: SourceItem) -> dict:
    return {
        "ref": source.ref,
        "channel": source.channel,
        "msg_id": source.msg_id,
        "timestamp": source.timestamp,
        "sender": source.sender,
        "url": source.url,
        "text": source.text,
    }


def _ensure_trailing_newline(text: str) -> str:
    return text if text.endswith("\n") else text + "\n"
