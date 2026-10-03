from chat_daily_tg.sanitize import sanitize_for_llm


def test_sanitize_for_llm_redacts_filter_sensitive_travel_terms():
    text = "护照先刷一页樱花签，然后拿樱花签刷美签；台湾护照和留學打工也被提到。"
    out = sanitize_for_llm(text)
    assert "护照" not in out
    assert "美签" not in out
    assert "台湾护照" not in out
    assert "留學打工" not in out
    assert out.count("[已脱敏]") >= 4


def test_sanitize_for_llm_redacts_authorization_and_key_assignments():
    secrets = [
        "header-secret-123456",
        "sk-proj-abcdefghijklmno",
        "yaml-secret-value",
        "json-secret-value",
        "google-header-secret",
    ]
    text = "\n".join([
        "POST /v1/models returned 401",
        f'Authorization: Bearer {secrets[0]}',
        f"fallback token is Bearer {secrets[1]}",
        f"api_key: {secrets[2]}  # CLIProxyAPI upstream",
        f'{{"apiKey": "{secrets[3]}", "model": "gpt-test"}}',
        f"x-goog-api-key={secrets[4]}",
        "retry remained disabled after the 401",
    ])

    out = sanitize_for_llm(text)

    assert all(secret not in out for secret in secrets)
    assert "Authorization: Bearer [凭据已脱敏]" in out
    assert '"apiKey": "[凭据已脱敏]"' in out
    assert "POST /v1/models returned 401" in out
    assert "CLIProxyAPI upstream" in out
    assert "retry remained disabled after the 401" in out


def test_sanitize_for_llm_redacts_common_prefixed_keys_without_labels():
    secrets = [
        "sk-abcdefghijklmnopqrstuvwxyz",
        "AIzaSyA-test-google-key-123456789012",
        "AKIAIOSFODNN7EXAMPLE",
        "ghp_abcdefghijklmnopqrstuvwxyz123456",
        "xoxb-test-sanitizer-placeholder-not-a-real-token",
    ]
    out = sanitize_for_llm("keys rotated: " + " ".join(secrets))

    assert all(secret not in out for secret in secrets)
    assert out.startswith("keys rotated: ")
    assert out.count("[凭据已脱敏]") == len(secrets)


def test_sanitize_for_llm_redacts_cookie_headers_and_preserves_next_line():
    text = (
        "403 from dashboard\n"
        "Cookie: session=top-secret; cf_clearance=also-secret\n"
        '"Set-Cookie": "sid=json-secret; HttpOnly"\n'
        "proxy exit was consistent"
    )

    out = sanitize_for_llm(text)

    assert "top-secret" not in out
    assert "also-secret" not in out
    assert "json-secret" not in out
    assert "Cookie: [凭据已脱敏]" in out
    assert '"Set-Cookie": "[凭据已脱敏]"' in out
    assert "403 from dashboard" in out
    assert "proxy exit was consistent" in out


def test_sanitize_for_llm_redacts_phone_numbers_but_not_dates_or_error_codes():
    text = (
        "联系人 13800138000，备用 +86 139-1234-5678，海外 +1 (415) 555-2671。"
        "日期 2026-08-25，HTTP 429，群 ID 1002957384858。"
    )

    out = sanitize_for_llm(text)

    assert "13800138000" not in out
    assert "139-1234-5678" not in out
    assert "415) 555-2671" not in out
    assert out.count("[手机号已脱敏]") == 3
    assert "2026-08-25" in out
    assert "HTTP 429" in out
    assert "1002957384858" in out
