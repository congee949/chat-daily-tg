"""Backward-compatible wrapper for the historical ``run_daily.py`` command.

The import alias deliberately exposes the application module itself.  Existing
scripts and tests that patch ``run_daily.<dependency>`` therefore keep affecting
the real orchestration during the staged package migration.
"""
from __future__ import annotations

import sys

from chat_daily_tg import application as _application


if __name__ == "__main__":
    raise SystemExit(_application.legacy_main())


sys.modules[__name__] = _application
