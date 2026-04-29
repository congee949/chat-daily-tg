from __future__ import annotations


def abbreviate_sources(text: str, mapping: dict[str, str]) -> str:
    """Replace long source names with abbreviations in citation tails.

    Citation formats supported:
      （群名 / HH:MM）
      （群名 / sender / HH:MM）
      （群A / HH:MM；群B / HH:MM）
    """
    if not mapping:
        return text

    # Sort by length descending so longer names are replaced first
    for full_name, short_name in sorted(mapping.items(), key=lambda x: len(x[0]), reverse=True):
        if not full_name or not short_name:
            continue
        # Replace inside parentheses citations only, not elsewhere
        text = text.replace(f"（{full_name} /", f"（{short_name} /")
        text = text.replace(f"；{full_name} /", f"；{short_name} /")
    return text


def post_process_concise(text: str, abbreviations: dict[str, str] | None = None) -> str:
    """Apply all post-processing steps to the concise markdown before sending."""
    text = abbreviate_sources(text, abbreviations or {})
    return text
