from __future__ import annotations
import re

_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)]+)\)")


def _md_links_to_html(text: str) -> str:
    """Convert Markdown [text](url) links to Telegram-compatible HTML."""
    def _replace(m: re.Match) -> str:
        label = m.group(1).replace("&", "&amp;").replace('"', "&quot;")
        url = m.group(2).replace("&", "&amp;")
        return f'<a href="{url}">{label}</a>'
    return _MD_LINK_RE.sub(_replace, text)


def abbreviate_sources(text: str, mapping: dict[str, str]) -> str:
    """Replace long source names with abbreviations in citation tails.

    Citation formats supported (no timestamps):
      （群名）
      （群名 / sender）
      （群A；群B）
    """
    if not mapping:
        return text

    # A source name in a citation is bounded on the left by （ or ；, and on the
    # right by ）, ；, or " /" (sender variant). Replace only in those contexts so
    # group names appearing in prose aren't touched.
    right_delims = ("）", "；", " /")
    # Sort by length descending so longer names are replaced first
    for full_name, short_name in sorted(mapping.items(), key=lambda x: len(x[0]), reverse=True):
        if not full_name or not short_name:
            continue
        for left in ("（", "；"):
            for right in right_delims:
                text = text.replace(f"{left}{full_name}{right}", f"{left}{short_name}{right}")
    return text


def post_process_concise(text: str, abbreviations: dict[str, str] | None = None) -> str:
    """Apply all post-processing steps to the concise markdown before sending."""
    text = abbreviate_sources(text, abbreviations or {})
    text = _md_links_to_html(text)
    return text
