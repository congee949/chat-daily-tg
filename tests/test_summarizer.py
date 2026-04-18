import pytest
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
    bad = "```markdown concise\nOnly one fence\n```"
    with pytest.raises(ValueError, match="detailed"):
        parse_summary_output(bad)


def test_parse_summary_output_handles_crlf():
    crlf_text = "```markdown concise\r\nconcise body\r\n```\r\n\r\n```markdown detailed\r\ndetailed body\r\n```\r\n\r\n```json opportunities\r\n{\"permanent_additions\": [], \"hot_leads_additions\": [], \"death_signals\": []}\r\n```"
    out = parse_summary_output(crlf_text)
    assert "concise body" in out.concise_md
    assert "\r" not in out.concise_md
    assert "detailed body" in out.detailed_md


def test_parse_summary_output_invalid_json_raises_valueerror():
    bad = """```markdown concise
c
```

```markdown detailed
d
```

```json opportunities
{bad json}
```"""
    with pytest.raises(ValueError, match="not valid JSON"):
        parse_summary_output(bad)
