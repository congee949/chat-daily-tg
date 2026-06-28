import pytest
from chat_daily_tg.summarizer import (
    VERIFIER_SYSTEM,
    _sanitize_json,
    parse_summary_output,
    parse_verified_summary_output,
    run_summary,
    SummaryOutput,
)


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

VERIFIED_OUTPUT = SAMPLE_OUTPUT + """

```json verification
{
  "checked_claims": [
    {
      "claim": "Claude 4.3 发布",
      "status": "downgraded",
      "reason": "原文只有 4.3，没有明确实体名",
      "evidence": ["G1 / 14:15: 4.3出了哦"],
      "confidence": "high"
    }
  ]
}
```"""


def test_parse_summary_output_extracts_three_sections():
    out = parse_summary_output(SAMPLE_OUTPUT)
    assert isinstance(out, SummaryOutput)
    assert "Test concise" in out.concise_md
    assert "Test detailed" in out.detailed_md
    assert out.opportunities["permanent_additions"] == []


def test_parse_summary_output_missing_concise_fence_raises():
    bad = "```markdown detailed\nOnly detailed\n```"
    with pytest.raises(ValueError, match="concise"):
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


def test_parse_verified_summary_output_extracts_verification():
    out = parse_verified_summary_output(VERIFIED_OUTPUT)

    assert out.verification is not None
    assert out.verification["checked_claims"][0]["status"] == "downgraded"
    assert out.verification["checked_claims"][0]["evidence"] == ["G1 / 14:15: 4.3出了哦"]


def test_parse_verified_summary_output_accepts_untagged_json_verification_fence():
    output = SAMPLE_OUTPUT + """

```json
{"checked_claims":[]}
```"""

    out = parse_verified_summary_output(output)

    assert out.verification == {"checked_claims": []}


def test_parse_verified_summary_output_requires_verification_fence():
    with pytest.raises(ValueError, match="json verification"):
        parse_verified_summary_output(SAMPLE_OUTPUT)


def test_run_summary_repairs_malformed_first_output_then_verifies(tmp_path):
    repaired = """```markdown concise
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
    verified = repaired + """

```json verification
{"checked_claims":[]}
```"""

    class FakeLLM:
        def __init__(self):
            self.calls = []

        def chat(self, prompt, system=None):
            self.calls.append((prompt, system))
            if len(self.calls) == 1:
                return "### 🌅 今日总览\n- missing fences", {}
            if len(self.calls) == 2:
                return repaired, {}
            return verified, {}

    detail_path = tmp_path / "summary.md"
    llm = FakeLLM()
    out = run_summary(
        llm_client=llm,
        date="2026-04-27",
        groups_with_content=[("微信 / G1", "content")],
        detail_path=str(detail_path),
    )

    assert out.concise_md.startswith("### 🌅")
    assert out.verification == {"checked_claims": []}
    assert len(llm.calls) == 3
    assert llm.calls[-1][1] == VERIFIER_SYSTEM
    assert "## 原始聊天记录" in llm.calls[-1][0]
    assert "## 日报初稿" in llm.calls[-1][0]
    assert (tmp_path / "llm-output-unparsed.md").exists()


def test_run_summary_passes_embedding_evidence_context_to_verifier(tmp_path):
    draft = SAMPLE_OUTPUT
    verified = VERIFIED_OUTPUT

    class FakeLLM:
        def __init__(self):
            self.calls = []

        def chat(self, prompt, system=None):
            self.calls.append((prompt, system))
            return (draft if len(self.calls) == 1 else verified), {}

    llm = FakeLLM()
    run_summary(
        llm_client=llm,
        date="2026-05-06",
        groups_with_content=[("Telegram / G1", "content")],
        detail_path=str(tmp_path / "summary.md"),
        evidence_context_builder=lambda output: "### Claim 查询：Claude 4.3\n- [1.000] G1 / 14:15 / A: 4.3出了哦",
    )

    verifier_prompt = llm.calls[1][0]
    assert "## Embedding 检索证据" in verifier_prompt
    assert "4.3出了哦" in verifier_prompt


def test_run_summary_skips_verifier_when_no_high_risk_claims(tmp_path):
    """Builder returning "" means zero high-risk claims were extracted from the
    draft — the verifier call is skipped and the draft is returned as-is with an
    empty verification record."""

    class FakeLLM:
        def __init__(self):
            self.calls = []

        def chat(self, prompt, system=None):
            self.calls.append((prompt, system))
            return SAMPLE_OUTPUT, {}

    llm = FakeLLM()
    out = run_summary(
        llm_client=llm,
        date="2026-05-06",
        groups_with_content=[("Telegram / G1", "content")],
        detail_path=str(tmp_path / "summary.md"),
        evidence_context_builder=lambda output: "",
    )

    assert len(llm.calls) == 1  # draft only, no verifier call
    assert out.verification == {"checked_claims": []}
    assert out.concise_md  # draft preserved


def test_run_summary_verifier_can_downgrade_ambiguous_entity(tmp_path):
    draft = """```markdown concise
### 🧠 AI / 工具
- **Claude 4.3 发布**：实时语音第一，TTS 不如 Gemini（G1 / 14:15）
```

```markdown detailed
## 全局重点
- Claude 4.3 发布。
```

```json opportunities
{"permanent_additions":[],"hot_leads_additions":[],"death_signals":[]}
```"""
    verified = """```markdown concise
### ⚠️ 风险 / 待验证
- **疑似某 4.3 模型发布**：原文只提到“4.3出了”和可读 X，未明确模型名，需外部验证（G1 / 14:15）
```

```markdown detailed
## 全局重点
- 原文提到“4.3出了”，但未明确是 Claude、Grok 或其他模型。
```

```json opportunities
{"permanent_additions":[],"hot_leads_additions":[],"death_signals":[]}
```

```json verification
{"checked_claims":[{"claim":"Claude 4.3 发布","status":"downgraded","reason":"原文没有 Claude 实体名","evidence":["G1 / 14:15: 4.3出了哦"],"confidence":"high"}]}
```"""

    class FakeLLM:
        def __init__(self):
            self.calls = []

        def chat(self, prompt, system=None):
            self.calls.append((prompt, system))
            return (draft if len(self.calls) == 1 else verified), {}

    out = run_summary(
        llm_client=FakeLLM(),
        date="2026-05-06",
        groups_with_content=[("Telegram / G1", "[Telegram / G1 / 14:15 / A] 4.3出了哦\n[Telegram / G1 / 14:22 / B] 这个能直接读x")],
        detail_path=str(tmp_path / "summary.md"),
    )

    assert "Claude 4.3" not in out.concise_md
    assert "疑似某 4.3 模型" in out.concise_md
    assert out.verification["checked_claims"][0]["status"] == "downgraded"


def test_prompt_requires_unified_source_tagged_concise_output():
    from chat_daily_tg.prompts import SUMMARIZER_SYSTEM

    assert "形式 A" in SUMMARIZER_SYSTEM
    assert "不要按微信/Telegram分块" in SUMMARIZER_SYSTEM
    assert "每条重点必须" in SUMMARIZER_SYSTEM
    assert "（群名）" in SUMMARIZER_SYSTEM
    assert "不要标注时间" in SUMMARIZER_SYSTEM
    assert "不要写 `微信 /` 或 `Telegram /`" in SUMMARIZER_SYSTEM
    assert "精简来源标签" in SUMMARIZER_SYSTEM
    assert "### 🌅 今日总览" in SUMMARIZER_SYSTEM
    assert "### 💰 钱 / 活动" in SUMMARIZER_SYSTEM
    assert "### 🧠 AI / 工具" in SUMMARIZER_SYSTEM
    assert "### ⚠️ 风险 / 待验证" in SUMMARIZER_SYSTEM
    assert "### 🔁 重复 / 旧闻" in SUMMARIZER_SYSTEM


def test_build_user_prompt_includes_group_only_concise_source_label():
    from chat_daily_tg.prompts import build_user_prompt

    prompt = build_user_prompt(
        date="2026-04-27",
        groups_with_content=[
            ("微信 / 示例微信群A", "content a"),
            ("Telegram / 示例TG群A", "content b"),
        ],
        detail_path="/tmp/summary.md",
    )

    assert "完整来源标签：微信 / 示例微信群A" in prompt
    assert "精简来源标签：示例微信群A" in prompt
    assert "完整来源标签：Telegram / 示例TG群A" in prompt
    assert "精简来源标签：示例TG群A" in prompt
    assert "不要写平台名" in prompt


def test_build_user_prompt_includes_repeat_context():
    from chat_daily_tg.prompts import build_user_prompt

    prompt = build_user_prompt(
        date="2026-04-28",
        groups_with_content=[("微信 / G1", "content")],
        detail_path="/tmp/summary.md",
        active_repeat_topics_summary="- `abc` [repeat] Codex 额度重置",
    )

    assert "近期已见话题" in prompt
    assert "Codex 额度重置" in prompt
    assert "重复/旧闻降权" in prompt


def test_sanitize_json_fixes_invalid_unicode_escape():
    bad = r'{"key": "hello \uzzzz world"}'
    result = _sanitize_json(bad)
    import json
    parsed = json.loads(result)
    assert "hello" in parsed["key"]


def test_sanitize_json_closes_truncated_braces():
    bad = '{"permanent_additions": [], "hot_leads_additions": ['
    result = _sanitize_json(bad)
    import json
    parsed = json.loads(result)
    assert isinstance(parsed, dict)


def test_sanitize_json_removes_trailing_comma():
    bad = '{"a": 1, "b": [2, 3,],}'
    result = _sanitize_json(bad)
    import json
    parsed = json.loads(result)
    assert parsed["a"] == 1


def test_parse_verified_summary_output_handles_invalid_u_escape_in_verification():
    raw = """```markdown concise
### 🌅 今日总览
- test
```

```markdown detailed
test
```

```json opportunities
{"permanent_additions":[],"hot_leads_additions":[],"death_signals":[]}
```

```json verification
{"checked_claims":[{"claim":"test \\uzzzz claim","status":"supported","reason":"ok","evidence":[],"confidence":"high"}]}
```"""
    out = parse_verified_summary_output(raw)
    assert out.verification is not None
    assert len(out.verification["checked_claims"]) == 1
