import pytest
from chat_daily_tg.summarizer import parse_summary_output, run_summary, SummaryOutput


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


def test_run_summary_repairs_malformed_first_output(tmp_path):
    good = """```markdown concise
### 🌅 今日总览
- ok
```

```markdown detailed
## 全局重点
ok
```

```json opportunities
{"permanent_additions":[],"hot_leads_additions":[],"death_signals":[]}
```"""

    class FakeLLM:
        def __init__(self):
            self.calls = []

        def chat(self, prompt, system=None):
            self.calls.append((prompt, system))
            if len(self.calls) == 1:
                return "### 🌅 今日总览\n- missing fences", {}
            return good, {}

    detail_path = tmp_path / "summary.md"
    llm = FakeLLM()
    out = run_summary(
        llm_client=llm,
        date="2026-04-27",
        groups_with_content=[("微信 / G1", "content")],
        detail_path=str(detail_path),
    )

    assert out.concise_md.startswith("### 🌅")
    assert len(llm.calls) == 2
    assert (tmp_path / "llm-output-unparsed.md").exists()


def test_prompt_requires_unified_source_tagged_concise_output():
    from chat_daily_tg.prompts import SUMMARIZER_SYSTEM

    assert "形式 A" in SUMMARIZER_SYSTEM
    assert "不要按微信/Telegram分块" in SUMMARIZER_SYSTEM
    assert "每条重点必须" in SUMMARIZER_SYSTEM
    assert "（群名 / HH:MM）" in SUMMARIZER_SYSTEM
    assert "不要写 `微信 /` 或 `Telegram /`" in SUMMARIZER_SYSTEM
    assert "精简来源标签" in SUMMARIZER_SYSTEM
    assert "### 🌅 今日总览" in SUMMARIZER_SYSTEM
    assert "### 💰 钱 / 活动" in SUMMARIZER_SYSTEM
    assert "### 🧠 AI / 工具" in SUMMARIZER_SYSTEM
    assert "### ⚠️ 风险 / 待验证" in SUMMARIZER_SYSTEM


def test_build_user_prompt_includes_group_only_concise_source_label():
    from chat_daily_tg.prompts import build_user_prompt

    prompt = build_user_prompt(
        date="2026-04-27",
        groups_with_content=[
            ("微信 / OpenCLI 交流群", "content a"),
            ("Telegram / CuiMao爱学习", "content b"),
        ],
        detail_path="/tmp/summary.md",
    )

    assert "完整来源标签：微信 / OpenCLI 交流群" in prompt
    assert "精简来源标签：OpenCLI 交流群" in prompt
    assert "完整来源标签：Telegram / CuiMao爱学习" in prompt
    assert "精简来源标签：CuiMao爱学习" in prompt
    assert "不要写平台名" in prompt
