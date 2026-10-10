import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from chat_daily_tg import relay_observe as ro
from chat_daily_tg.raw_channels import _l2_check

NOW = datetime.now(timezone.utc)

ANTHROPIC_EN = (
    "We're beginning a process of publishing more frequent reports on model behavior. "
    "Today's report describes four types of behaviors we've identified during evaluations "
    "and internal use. Claude acted on real websites or systems in ways we didn't intend.")
ANTHROPIC_CN = (
    "Anthropic 暂停内部评测的模型网络访问。Anthropic 披露 Claude 在评测和内部使用中曾出现"
    "四类非预期行为：利用软件漏洞运行服务器命令、误提交真实表单、绕过限制而不是停下。"
    "公司表示这些案例的实际影响很小，将更频繁地发布模型行为报告。")
HAIKU_EN = ("Introducing Claude Haiku 5.5: the cheapest, fastest, and most capable small model "
            "we've ever released. It costs around 75% less to run than Claude Haiku 4.5.")
HAIKU_CN = ("Anthropic 发布 Haiku 5.5 小型模型。Anthropic 正式发布 Claude Haiku 5.5，定位速度最快、"
            "成本最低的小模型，平均运行成本比 Haiku 4.5 低约 75%，已在 API 和三大云平台上线。")
HK_CN = ("香港证监会：将就延长交易时段发讨论文件。香港证监会行政总裁 10 月 9 日表示，"
         "将研究延长证券交易时段，并率先在衍生产品市场推行。")


def x_row(author, tid, text, hours_ago=2):
    return {"schema": "sent-content.v1", "producer": "x_monitor", "delivery_state": "confirmed",
            "source_ref": f"https://x.com/{author}/status/{tid}", "message_id": int(tid) % 100000,
            "content_id": f"x-tweet:{tid}", "content": f"📢 @{author}​\n\n{text}",
            "sent_at": (NOW - timedelta(hours=hours_ago)).isoformat()}


class FakeLLM:
    model = "fake"

    def __init__(self, answer):
        self.answer = answer
        self.prompts = []

    def chat(self, prompt, system=None):
        self.prompts.append(prompt)
        if isinstance(self.answer, Exception):
            raise self.answer
        return (self.answer if isinstance(self.answer, str)
                else json.dumps(self.answer, ensure_ascii=False)), {}


@pytest.fixture
def snapshot(tmp_path):
    rows = [x_row("AnthropicAI", "2108680150556737819", ANTHROPIC_EN),
            x_row("claudeai", "2107896388163000001", HAIKU_EN),
            x_row("OpenAI", "2107896388163000002", "Old post", hours_ago=100)]
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps({"schema": "x", "rows": rows}), encoding="utf-8")
    return path


def observer(snapshot, tmp_path, answer, **kw):
    return ro.RelayObserver(llm=FakeLLM(answer), snapshot_path=snapshot,
                            journal_path=tmp_path / "relay.jsonl",
                            channels=["-1001125107539"], **kw)


CHANNEL = SimpleNamespace(id="-1001125107539", name="科技圈🎗在花频道📮", dedup=True)


def test_load_recent_keeps_window(snapshot):
    assert [ro.row_author(r) for r in ro.load_recent(snapshot)] == ["AnthropicAI", "claudeai"]
    assert ro.load_recent(snapshot.with_name("missing.json")) == []


def test_retrieval_matches_cross_language_and_skips_unrelated(snapshot):
    rows = ro.load_recent(snapshot)
    assert [ro.row_author(c["row"]) for c in ro.retrieve(HAIKU_CN, rows)] == ["claudeai"]
    assert ro.retrieve(HK_CN, rows) == []


def test_decide_mapping():
    cands = [{"row": x_row("a", "1000001", "x")}]
    base = {"same_event": True, "matched": 1, "added": "background", "confidence": 0.9}
    assert ro.decide(base, cands)[0] == "drop"
    assert ro.decide(dict(base, added="none"), cands)[0] == "drop"
    assert ro.decide(dict(base, added="substantive"), cands)[0] == "fold"
    for bad in ({"same_event": False}, {"confidence": 0.5}, {"confidence": "0.9"},
                {"matched": 2}, {"matched": True}, {"added": "other"}):
        assert ro.decide(dict(base, **bad), cands)[0] == "keep"


def test_observe_writes_drop_record(snapshot, tmp_path):
    obs = observer(snapshot, tmp_path, "结论如下：\n" + json.dumps(
        {"same_event": True, "matched": 1, "added": "background", "delta": "",
         "confidence": 0.92, "reason": "转述官方报告"}, ensure_ascii=False))
    record = obs.observe(CHANNEL, HAIKU_CN, [44317])
    assert record["decision"] == "drop"
    assert record["matched"]["author"] == "claudeai"
    saved = [json.loads(line) for line in (tmp_path / "relay.jsonl").read_text().splitlines()]
    assert saved[0]["msg_ids"] == [44317] and saved[0]["mode"] == "observe"
    assert HAIKU_EN[:40] in obs.llm.prompts[0]


def test_unconfigured_channel_and_ineligible_text_are_silent(snapshot, tmp_path):
    obs = observer(snapshot, tmp_path, {})
    other = SimpleNamespace(id="-100999", name="other", dedup=True)
    assert obs.observe(other, HAIKU_CN, [1]) is None
    assert obs.observe(CHANNEL, "short", [1])["reason"] == "not_eligible"
    assert not (tmp_path / "relay.jsonl").exists()
    assert obs.llm.prompts == []


def test_failures_and_budget_keep(snapshot, tmp_path):
    obs = observer(snapshot, tmp_path, RuntimeError("boom"), max_calls=1)
    assert obs.observe(CHANNEL, HAIKU_CN, [1])["reason"] == "ai_failed:RuntimeError"
    assert obs.observe(CHANNEL, HAIKU_CN, [2])["reason"] == "ai_budget"
    assert observer(snapshot, tmp_path, "not json").observe(
        CHANNEL, HAIKU_CN, [3])["reason"].startswith("ai_failed:")


def test_l2_check_calls_hook_without_changing_delivery(snapshot, tmp_path):
    obs = observer(snapshot, tmp_path, {"same_event": True, "matched": 1, "added": "none",
                                        "confidence": 0.95, "reason": "翻译"})
    token = ro.current_observer.set(obs)
    try:
        assert _l2_check(None, CHANNEL, HAIKU_CN, [44317], seen=None) == (False, "", None)
    finally:
        ro.current_observer.reset(token)
    assert json.loads((tmp_path / "relay.jsonl").read_text())["decision"] == "drop"
    # Without an installed observer the hook is a no-op.
    assert _l2_check(None, CHANNEL, HAIKU_CN, [44318], seen=None) == (False, "", None)


def test_hook_swallows_observer_errors():
    class Broken:
        def observe(self, *args):
            raise RuntimeError("boom")
    token = ro.current_observer.set(Broken())
    try:
        ro.observe_card(CHANNEL, HAIKU_CN, [1])
    finally:
        ro.current_observer.reset(token)


def test_l2_nearest_x_delivery_becomes_candidate(snapshot, tmp_path):
    obs = observer(snapshot, tmp_path, {"same_event": True, "matched": 1, "added": "background",
                                        "confidence": 0.9, "reason": "转述官方报告"})
    assert ro.retrieve(ANTHROPIC_CN, ro.load_recent(snapshot)) == []
    verdict = SimpleNamespace(matched_msg_id=37819, similarity=0.86)
    record = obs.observe(CHANNEL, ANTHROPIC_CN, [44317], l2_verdict=verdict)
    assert record["decision"] == "drop"
    assert record["candidates"][0]["l2_similarity"] == 0.86
    assert record["matched"]["author"] == "AnthropicAI"


def test_l2_match_outside_x_deliveries_is_ignored(snapshot, tmp_path):
    obs = observer(snapshot, tmp_path, {})
    record = obs.observe(CHANNEL, ANTHROPIC_CN, [1],
                         l2_verdict=SimpleNamespace(matched_msg_id=99999, similarity=0.9))
    assert record["reason"] == "no_candidate"
    assert obs.llm.prompts == []
