from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from time import perf_counter
from typing import Literal
import csv

from chat_daily_tg.prompts import SUMMARIZER_SYSTEM, build_user_prompt
from chat_daily_tg.summarizer import SummaryOutput, parse_summary_output
from chat_daily_tg.tg_sender import (
    TelegramSender,
    format_html_for_telegram,
    format_markdownish_for_telegram,
    split_message,
)


ParseMode = Literal["HTML", "MarkdownV2", "none"]

RESULTS_FIELDS = [
    "timestamp",
    "experiment_id",
    "date",
    "fixture_count",
    "fixture_names",
    "model",
    "max_tokens",
    "parse_mode",
    "status",
    "elapsed_seconds",
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "raw_chars",
    "concise_chars",
    "detailed_chars",
    "telegram_chunks",
    "telegram_status",
    "telegram_message_ids",
    "markdownv2_render_status",
    "truncated_suspected",
    "error",
    "notes",
]


@dataclass(frozen=True)
class ResearchSpec:
    experiment_id: str
    date: str
    fixture_paths: list[Path]
    detail_path: str
    model: str
    max_tokens: int
    parse_mode: ParseMode = "HTML"
    notes: str = ""


@dataclass(frozen=True)
class ResearchResult:
    timestamp: str
    experiment_id: str
    date: str
    fixture_count: int
    fixture_names: str
    model: str
    max_tokens: int
    parse_mode: str
    status: str
    elapsed_seconds: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    raw_chars: int
    concise_chars: int
    detailed_chars: int
    telegram_chunks: int
    telegram_status: str
    telegram_message_ids: str
    markdownv2_render_status: str
    truncated_suspected: str
    error: str
    notes: str

    def to_row(self) -> dict[str, object]:
        return asdict(self)


def load_fixture_groups(paths: list[Path]) -> list[tuple[str, str]]:
    groups: list[tuple[str, str]] = []
    for path in paths:
        groups.append((path.stem, path.read_text(encoding="utf-8")))
    return groups


def append_result(path: Path, result: ResearchResult) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    needs_header = not path.exists() or path.stat().st_size == 0
    with path.open("a", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=RESULTS_FIELDS, dialect="excel-tab")
        if needs_header:
            writer.writeheader()
        writer.writerow(result.to_row())


def run_research_once(
    spec: ResearchSpec,
    *,
    llm_client=None,
    sample_output: str | None = None,
    telegram_sender: TelegramSender | None = None,
    send_telegram: bool = False,
) -> ResearchResult:
    started = perf_counter()
    timestamp = datetime.now().isoformat(timespec="seconds")
    fixture_names = ",".join(str(path) for path in spec.fixture_paths)
    raw_text = ""
    usage: dict = {}

    try:
        if sample_output is None:
            if llm_client is None:
                raise ValueError("llm_client is required when sample_output is not provided")
            groups = load_fixture_groups(spec.fixture_paths)
            user_prompt = build_user_prompt(
                date=spec.date,
                groups_with_content=groups,
                detail_path=spec.detail_path,
            )
            raw_text, usage = llm_client.chat(user_prompt, system=SUMMARIZER_SYSTEM)
        else:
            raw_text = sample_output

        parsed = parse_summary_output(raw_text)
        render_text, render_status, render_error = render_for_telegram(parsed, spec.parse_mode)
        telegram_status = "dry_run"
        message_ids: list[int] = []
        if send_telegram:
            if telegram_sender is None:
                raise ValueError("telegram_sender is required when send_telegram is true")
            message_ids = telegram_sender.send(parsed.concise_md, parse_mode=_parse_mode_arg(spec.parse_mode))
            telegram_status = "sent"

        status = "ok" if render_error == "" else "render_error"
        return _result(
            spec=spec,
            timestamp=timestamp,
            started=started,
            fixture_names=fixture_names,
            status=status,
            usage=usage,
            raw_text=raw_text,
            parsed=parsed,
            telegram_chunks=len(split_message(render_text)),
            telegram_status=telegram_status,
            message_ids=message_ids,
            markdownv2_render_status=render_status,
            truncated_suspected="no",
            error=render_error,
        )
    except Exception as exc:
        return _result(
            spec=spec,
            timestamp=timestamp,
            started=started,
            fixture_names=fixture_names,
            status=_status_for_error(exc),
            usage=usage,
            raw_text=raw_text,
            parsed=None,
            telegram_chunks=0,
            telegram_status="not_attempted",
            message_ids=[],
            markdownv2_render_status="not_attempted",
            truncated_suspected=_truncation_guess(raw_text, exc),
            error=f"{type(exc).__name__}: {exc}",
        )


def render_for_telegram(output: SummaryOutput, parse_mode: ParseMode) -> tuple[str, str, str]:
    try:
        if parse_mode == "MarkdownV2":
            return format_markdownish_for_telegram(output.concise_md), "ok", ""
        if parse_mode == "HTML":
            return format_html_for_telegram(output.concise_md), "not_applicable", ""
        return output.concise_md, "not_applicable", ""
    except Exception as exc:
        return "", "error", f"{type(exc).__name__}: {exc}"


def _parse_mode_arg(parse_mode: ParseMode) -> str | None:
    if parse_mode == "none":
        return None
    return parse_mode


def _status_for_error(exc: Exception) -> str:
    if isinstance(exc, ValueError) and "fence" in str(exc):
        return "parse_error"
    return "error"


def _truncation_guess(raw_text: str, exc: Exception) -> str:
    message = str(exc)
    if "missing fence" in message:
        return "yes"
    if raw_text and not raw_text.rstrip().endswith("```"):
        return "yes"
    return "unknown"


def _result(
    *,
    spec: ResearchSpec,
    timestamp: str,
    started: float,
    fixture_names: str,
    status: str,
    usage: dict,
    raw_text: str,
    parsed: SummaryOutput | None,
    telegram_chunks: int,
    telegram_status: str,
    message_ids: list[int],
    markdownv2_render_status: str,
    truncated_suspected: str,
    error: str,
) -> ResearchResult:
    return ResearchResult(
        timestamp=timestamp,
        experiment_id=spec.experiment_id,
        date=spec.date,
        fixture_count=len(spec.fixture_paths),
        fixture_names=fixture_names,
        model=spec.model,
        max_tokens=spec.max_tokens,
        parse_mode=spec.parse_mode,
        status=status,
        elapsed_seconds=f"{perf_counter() - started:.3f}",
        prompt_tokens=int(usage.get("prompt_tokens", 0) or 0),
        completion_tokens=int(usage.get("completion_tokens", 0) or 0),
        total_tokens=int(usage.get("total_tokens", 0) or 0),
        raw_chars=len(raw_text),
        concise_chars=len(parsed.concise_md) if parsed else 0,
        detailed_chars=len(parsed.detailed_md) if parsed else 0,
        telegram_chunks=telegram_chunks,
        telegram_status=telegram_status,
        telegram_message_ids=",".join(str(message_id) for message_id in message_ids),
        markdownv2_render_status=markdownv2_render_status,
        truncated_suspected=truncated_suspected,
        error=error,
        notes=spec.notes,
    )
