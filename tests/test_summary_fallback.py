from chat_daily_tg.application import _run_summary_with_fallback


class _LLM:
    def __init__(self, model: str):
        self.model = model


def test_summary_restarts_full_pipeline_with_next_provider_after_failure():
    calls = []

    def run_fn(*, llm_client, **_kwargs):
        calls.append(llm_client.model)
        if llm_client.model == "claude-sonnet-4-6":
            raise RuntimeError("verifier unavailable")
        return "qwen summary"

    result = _run_summary_with_fallback(
        run_fn,
        [("primary", _LLM("claude-sonnet-4-6")), ("qwenproxy", _LLM("Qwen3.7-Plus")),
         ("gemini", _LLM("gemini-3.6-flash-high"))],
        date="2026-07-25",
    )

    assert result == "qwen summary"
    assert calls == ["claude-sonnet-4-6", "Qwen3.7-Plus"]


def test_summary_reraises_final_provider_failure():
    def run_fn(*, llm_client, **_kwargs):
        raise RuntimeError(f"{llm_client.model} unavailable")

    try:
        _run_summary_with_fallback(
            run_fn, [("primary", _LLM("sonnet")), ("qwenproxy", _LLM("qwen"))]
        )
    except RuntimeError as error:
        assert str(error) == "qwen unavailable"
    else:
        raise AssertionError("the final provider failure must be visible")
