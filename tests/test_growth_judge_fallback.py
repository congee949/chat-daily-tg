from chat_daily_tg.application import _judge_growth_cards


class _LLM:
    def __init__(self, model: str):
        self.model = model


def test_growth_judge_uses_fallback_after_primary_error():
    calls = []

    def judge_fn(llm, card_a, card_b, rubric):
        calls.append(llm.model)
        if llm.model == "opus":
            raise RuntimeError("capacity exhausted")
        return {"winner": "B", "reason": "fallback verdict"}

    result = _judge_growth_cards(
        judge_fn, _LLM("opus"), _LLM("gemini"), "A", "B", "rubric")

    assert result["winner"] == "B"
    assert calls == ["opus", "gemini"]


def test_growth_judge_reraises_without_fallback():
    def judge_fn(*_args):
        raise RuntimeError("capacity exhausted")

    try:
        _judge_growth_cards(judge_fn, _LLM("opus"), None, "A", "B", "rubric")
    except RuntimeError as error:
        assert str(error) == "capacity exhausted"
    else:
        raise AssertionError("primary failure must remain visible without fallback")
