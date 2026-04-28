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


def apply_death_signals(
    signals: list[dict], db_path: Path, hot_leads_root: Path,
) -> int:
    """Update permanent DB and hot-leads jsonl based on LLM-returned signals.

    Returns number of entries updated.
    """
    db = PermanentDB(db_path)
    updated = 0
    perm_by_id = {e.id: e for e in db.read_all()}
    perm_title_to_id = {e.title: e.id for e in perm_by_id.values()}

    hot_leads = load_all_leads(hot_leads_root) if hot_leads_root.exists() else []
    hot_by_id = {l.id: l for l in hot_leads}
    hot_title_to_id = {l.title: l.id for l in hot_leads}

    for sig in signals:
        conf = sig.get("confidence", "low").lower()
        status = CONFIDENCE_TO_STATUS.get(conf)
        if status is None:
            log.info("death signal low confidence, ignored: %s", sig)
            continue
        target = sig.get("target_title_or_id", "").strip()
        if not target:
            continue
        signal_text = sig.get("signal_text", "")

        pid = target if target in perm_by_id else perm_title_to_id.get(target)
        if pid:
            if db.mark_status(pid, status=status, death_signal=signal_text):
                updated += 1
                log.info("marked permanent %s → %s (%s)", pid, status, signal_text)
                continue

        hid = target if target in hot_by_id else hot_title_to_id.get(target)
        if hid:
            if mark_lead_status(hot_leads_root, hid, status=status,
                                death_signal=signal_text):
                updated += 1
                log.info("marked hot_lead %s → %s (%s)", hid, status, signal_text)
                continue

        log.warning("death signal target not found: %s", target)

    return updated
