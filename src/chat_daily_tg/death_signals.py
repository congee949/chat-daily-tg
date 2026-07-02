from __future__ import annotations
import logging
from pathlib import Path
from chat_daily_tg.db import PermanentDB
from chat_daily_tg.hot_leads import load_all_leads, mark_lead_status

log = logging.getLogger(__name__)


CONFIDENCE_TO_STATUS = {
    "high": "dead",
    "medium": "likely_dead",
    "low": None,   # ignore
}


def _resolve(target: str, ids: set[str], title_to_ids: dict[str, list[str]]) -> str | None:
    """Resolve an LLM-provided target to a single id.

    Exact id wins. Otherwise match by title only when the title is unambiguous —
    a title shared by >1 entry is refused (review finding #6: a {title: id} map
    silently routed death signals to whichever entry was indexed last).
    """
    if target in ids:
        return target
    matches = title_to_ids.get(target, [])
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        log.warning("death signal target title ambiguous (%d entries), refusing title match: %s",
                    len(matches), target)
    return None


def apply_death_signals(
    signals: list[dict], db_path: Path, hot_leads_db: Path,
) -> int:
    """Update permanent DB and hot leads based on LLM-returned signals.

    Returns number of entries updated.
    """
    db = PermanentDB(db_path)
    updated = 0

    perm_entries = list(db.read_all())
    perm_ids = {e.id for e in perm_entries}
    perm_title_to_ids: dict[str, list[str]] = {}
    for e in perm_entries:
        perm_title_to_ids.setdefault(e.title, []).append(e.id)

    hot_leads = load_all_leads(hot_leads_db)
    hot_ids = {l.id for l in hot_leads}
    hot_title_to_ids: dict[str, list[str]] = {}
    for l in hot_leads:
        hot_title_to_ids.setdefault(l.title, []).append(l.id)

    for sig in signals:
        if not isinstance(sig, dict):
            continue
        conf = sig.get("confidence") or "low"
        conf = conf.lower() if isinstance(conf, str) else "low"
        status = CONFIDENCE_TO_STATUS.get(conf)
        if status is None:
            log.info("death signal low confidence, ignored: %s", sig)
            continue
        target = sig.get("target_title_or_id")
        target = target.strip() if isinstance(target, str) else ""
        if not target:
            continue
        signal_text = sig.get("signal_text") or ""

        pid = _resolve(target, perm_ids, perm_title_to_ids)
        if pid:
            if db.mark_status(pid, status=status, death_signal=signal_text):
                updated += 1
                log.info("marked permanent %s → %s (%s)", pid, status, signal_text)
                continue

        hid = _resolve(target, hot_ids, hot_title_to_ids)
        if hid:
            if mark_lead_status(hot_leads_db, hid, status=status,
                                death_signal=signal_text):
                updated += 1
                log.info("marked hot_lead %s → %s (%s)", hid, status, signal_text)
                continue

        log.warning("death signal target not found: %s", target)

    return updated
