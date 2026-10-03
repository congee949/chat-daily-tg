import logging
import re

import pytest

from chat_daily_tg.application import _stage_timing


def test_stage_timing_logs_stage_and_seconds(caplog):
    with caplog.at_level(logging.INFO, logger="run_daily"):
        with _stage_timing("vision"):
            pass
    lines = [r.getMessage() for r in caplog.records]
    assert any(re.fullmatch(r"stage timing: vision \d+\.\ds", m) for m in lines)


def test_stage_timing_logs_even_when_stage_raises(caplog):
    with caplog.at_level(logging.INFO, logger="run_daily"):
        with pytest.raises(RuntimeError, match="boom"):
            with _stage_timing("push"):
                raise RuntimeError("boom")
    assert any(re.fullmatch(r"stage timing: push \d+\.\ds", r.getMessage())
               for r in caplog.records)
