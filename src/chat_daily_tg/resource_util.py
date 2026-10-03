"""Resource ownership for optional enhancements that must not block delivery."""

from contextlib import ExitStack
import logging
from typing import Any

log = logging.getLogger(__name__)


def enter_optional_client(stack: ExitStack, context: Any) -> Any:
    client = context.__enter__()

    def close() -> None:
        try:
            context.__exit__(None, None, None)
        except Exception:
            log.warning("optional client cleanup failed", exc_info=True)

    stack.callback(close)
    return client
