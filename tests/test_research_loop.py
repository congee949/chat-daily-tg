from pathlib import Path

from chat_daily_tg.research_loop import (
    ResearchSpec,
    append_result,
    load_fixture_groups,
    run_research_once,
)


class FakeLLM:
    def __init__(self, output: str):
        self.output = output

    def chat(self, prompt: str, system: str | None = None):
        assert "今日原始聊天记录" in prompt
        assert system
        return self.output, {
            "prompt_tokens": 10,
            "completion_tokens": 20,
            "total_tokens": 30,
        }


def test_load_fixture_groups_uses_file_stem(tmp_path: Path):
    fixture = tmp_path / "group_a.md"
    fixture.write_text("hello", encoding="utf-8")

    assert load_fixture_groups([fixture]) == [("group_a", "hello")]


def test_run_research_once_with_sample_output_records_dry_run_metrics():
    sample = Path("tests/fixtures/summary_output_sample.txt").read_text(encoding="utf-8")
    spec = ResearchSpec(
        experiment_id="sample-html",
        date="2026-04-17",
        fixture_paths=[Path("tests/fixtures/wx_export_raw_sample.md")],
        detail_path="output/research/summary.md",
        model="sample-output",
        max_tokens=0,
        parse_mode="HTML",
    )

    result = run_research_once(spec, sample_output=sample)

    assert result.status == "ok"
    assert result.telegram_status == "dry_run"
    assert result.telegram_chunks == 1
    assert result.concise_chars > 0
    assert result.detailed_chars > 0
    assert result.truncated_suspected == "no"


def test_run_research_once_records_usage_from_llm():
    sample = Path("tests/fixtures/summary_output_sample.txt").read_text(encoding="utf-8")
    spec = ResearchSpec(
        experiment_id="fake-llm",
        date="2026-04-17",
        fixture_paths=[Path("tests/fixtures/wx_export_raw_sample.md")],
        detail_path="output/research/summary.md",
        model="fake",
        max_tokens=16000,
        parse_mode="MarkdownV2",
    )

    result = run_research_once(spec, llm_client=FakeLLM(sample))

    assert result.status == "ok"
    assert result.total_tokens == 30
    assert result.markdownv2_render_status == "ok"


def test_run_research_once_marks_missing_concise_fence_as_parse_error():
    spec = ResearchSpec(
        experiment_id="truncated",
        date="2026-04-17",
        fixture_paths=[Path("tests/fixtures/wx_export_raw_sample.md")],
        detail_path="output/research/summary.md",
        model="sample-output",
        max_tokens=8000,
        parse_mode="HTML",
    )

    result = run_research_once(spec, sample_output="```markdown detailed\nunfinished")

    assert result.status == "parse_error"
    assert result.truncated_suspected == "yes"
    assert "missing fence" in result.error


def test_append_result_writes_tsv_header_and_row(tmp_path: Path):
    sample = Path("tests/fixtures/summary_output_sample.txt").read_text(encoding="utf-8")
    spec = ResearchSpec(
        experiment_id="write-tsv",
        date="2026-04-17",
        fixture_paths=[Path("tests/fixtures/wx_export_raw_sample.md")],
        detail_path="output/research/summary.md",
        model="sample-output",
        max_tokens=0,
        parse_mode="HTML",
    )
    result = run_research_once(spec, sample_output=sample)
    out = tmp_path / "results.tsv"

    append_result(out, result)

    rows = out.read_text(encoding="utf-8").splitlines()
    assert rows[0].startswith("timestamp\texperiment_id\tdate")
    assert "\twrite-tsv\t" in rows[1]
