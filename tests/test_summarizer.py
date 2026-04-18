from wx_daily_tg.summarizer import parse_summary_output, SummaryOutput


SAMPLE_OUTPUT = """```markdown concise
### 🗓️ 日期概览
Test concise
### 📌 值得关注
- item 1
```

```markdown detailed
## 群 1
Test detailed
```

```json opportunities
{
  "permanent_additions": [],
  "hot_leads_additions": [],
  "death_signals": []
}
```"""


def test_parse_summary_output_extracts_three_sections():
    out = parse_summary_output(SAMPLE_OUTPUT)
    assert isinstance(out, SummaryOutput)
    assert "Test concise" in out.concise_md
    assert "Test detailed" in out.detailed_md
    assert out.opportunities["permanent_additions"] == []


def test_parse_summary_output_missing_fence_raises():
    import pytest
    bad = "```markdown concise\nOnly one fence\n```"
    with pytest.raises(ValueError, match="detailed"):
        parse_summary_output(bad)
