from __future__ import annotations

import re


_LLM_RISK_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in [
        r"护照",
        r"签证",
        r"美签",
        r"留學打工",
        r"留学打工",
        r"台湾护照",
        r"豬肝护照",
        r"猪肝护照",
    ]
]


def sanitize_for_llm(text: str) -> str:
    """Redact phrases that commonly trip hosted LLM content filters.

    This only changes the model prompt. Raw source exports remain archived.
    """
    out = text
    for pattern in _LLM_RISK_PATTERNS:
        out = pattern.sub("[已脱敏]", out)
    return out
