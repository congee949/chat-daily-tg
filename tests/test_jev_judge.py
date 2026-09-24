from dataclasses import asdict
import json
from types import SimpleNamespace

import httpx
import pytest

from chat_daily_tg.config import DedupTopic
from chat_daily_tg.jev_client import JevClient, JevError
from chat_daily_tg.jev_judge import JevJudge, build_jev_judge
from chat_daily_tg.topic_dedup import JudgeVerdict


def _matches():
    return [SimpleNamespace(msg_id=1, text="已送达的事件正文")]


def _response(p=0.9, info="minor"):
    return {
        "model": "jev-1.13.0",
        "answers": {
            "same_event": {"type": "noul", "noul": p},
            "new_info": {
                "type": "choice", "choice": info, "confidence": 1,
                "probabilities": {name: int(name == info) for name in
                                  ("none", "minor", "substantial", "uncertain")},
            },
        },
        "usage": {"input_tokens": 5, "output_tokens": 2},
    }


def _client(response=None, handler=None, attempts=1):
    transport = httpx.MockTransport(handler or (lambda _: httpx.Response(200, json=response)))
    return JevClient("test-secret", client=httpx.Client(transport=transport),
                     retry_max_attempts=attempts)


class Fallback:
    def __init__(self, error=False):
        self.calls = 0
        self.error = error

    def judge(self, *_):
        self.calls += 1
        if self.error:
            raise RuntimeError("private body")
        return JudgeVerdict(False, "substantial", "private prose", True)


@pytest.mark.parametrize("p,info,same", [(0.8, "minor", True), (0.1, "substantial", False),
                                         (0.5, "none", True)])
def test_primary_typed_result_is_used_without_fallback(tmp_path, p, info, same):
    fallback = Fallback()
    path = tmp_path / "judge.jsonl"
    judge = JevJudge(_client(_response(p, info)), path=path, fallback=fallback)
    verdict = judge.judge("新正文 authorization: Bearer sk-secret", _matches())
    assert verdict.ok and verdict.same_event is same and verdict.new_info == info
    assert fallback.calls == 0
    row = json.loads(path.read_text())
    assert row["status"] == "ok" and row["verdict"] == asdict(verdict)
    assert row["candidate_msg_ids"] == [1]
    assert row["model"] == "jev-1.13.0"
    assert "新正文" not in path.read_text() and "sk-secret" not in path.read_text()


@pytest.mark.parametrize("failure", ["timeout", "http", "malformed", "uncertain"])
def test_failure_and_uncertainty_use_fallback(tmp_path, failure):
    def handler(_):
        if failure == "timeout":
            raise httpx.ReadTimeout("private body")
        if failure == "http":
            return httpx.Response(403)
        if failure == "malformed":
            return httpx.Response(200, json=_response(5))
        return httpx.Response(200, json=_response(info="uncertain"))
    fallback = Fallback()
    path = tmp_path / "judge.jsonl"
    verdict = JevJudge(_client(handler=handler), path=path, fallback=fallback).judge("正文", _matches())
    assert verdict.ok and not verdict.same_event
    assert fallback.calls == 1 and verdict.reason.startswith("jev-fallback:")
    assert json.loads(path.read_text())["fallback_used"]
    assert "private" not in path.read_text()


def test_both_judges_fail_leaves_gate_fail_open(tmp_path):
    judge = JevJudge(_client(handler=lambda _: httpx.Response(403)), path=tmp_path/"j.jsonl",
                     fallback=Fallback(error=True))
    verdict = judge.judge("新正文", _matches())
    assert not verdict.ok and not verdict.same_event and verdict.new_info == "substantial"


def test_daily_and_per_run_budgets_include_failed_attempts(tmp_path):
    calls = []
    def handler(_):
        calls.append(1)
        return httpx.Response(503)
    path = tmp_path/"judge.jsonl"
    fallback = Fallback()
    first = JevJudge(_client(handler=handler, attempts=2), path=path, fallback=fallback,
                     max_attempts_per_run=2, daily_cap=2)
    first.judge("一", _matches())
    first.judge("二", _matches())
    second = JevJudge(_client(handler=handler, attempts=2), path=path, fallback=fallback,
                      max_attempts_per_run=2, daily_cap=2)
    second.judge("三", _matches())
    assert len(calls) == 2 and fallback.calls == 3
    assert json.loads(path.with_suffix(".budget.json").read_text())["count"] == 2
    assert [json.loads(s)["status"] for s in path.read_text().splitlines()] == [
        "error", "budget_exhausted", "budget_exhausted"]


def test_journal_failure_preserves_successful_verdict(tmp_path, monkeypatch):
    judge = JevJudge(_client(_response()), path=tmp_path/"judge.jsonl")
    # The budget remains usable while just the append target is unavailable.
    judge.path.mkdir()
    verdict = judge.judge("新正文", _matches())
    assert verdict.ok and verdict.same_event


def _cfg(topic=None, model=None):
    return SimpleNamespace(models=SimpleNamespace(jev=model or {"enabled": True}),
                           sources=SimpleNamespace(telegram=SimpleNamespace(
                               dedup=SimpleNamespace(topic=topic or DedupTopic(judge_provider="jev")))))


def test_factory_and_provider_selector(monkeypatch, tmp_path):
    from chat_daily_tg import paths
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    monkeypatch.setattr(paths, "JEV_DEDUP_JUDGE", tmp_path/"judge.jsonl")
    cfg = _cfg()
    assert isinstance(build_jev_judge(cfg), JevJudge)
    cfg.sources.telegram.dedup.topic.judge_provider = "llm"
    assert build_jev_judge(cfg) is None


@pytest.mark.parametrize("model,policy", [
    ({"enabled": False}, {}), ({"enabled": True, "endpoint": "https://invalid.example"}, {}),
    ({"enabled": True, "zero_data_retention": True}, {}),
    ({"enabled": True}, {"jev_judge_daily_cap": 51}),
    ({"enabled": True}, {"jev_same_event_threshold": float("nan")}),
])
def test_invalid_primary_settings_leave_fallback_available(monkeypatch, model, policy):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    assert build_jev_judge(_cfg(DedupTopic(judge_provider="jev", **policy), model)) is None
