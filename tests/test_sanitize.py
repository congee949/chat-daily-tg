from chat_daily_tg.sanitize import sanitize_for_llm


def test_sanitize_for_llm_redacts_filter_sensitive_travel_terms():
    text = "护照先刷一页樱花签，然后拿樱花签刷美签；台湾护照和留學打工也被提到。"
    out = sanitize_for_llm(text)
    assert "护照" not in out
    assert "美签" not in out
    assert "台湾护照" not in out
    assert "留學打工" not in out
    assert out.count("[已脱敏]") >= 4
