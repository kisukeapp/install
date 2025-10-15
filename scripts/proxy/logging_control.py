"""Runtime toggle for proxy request logging."""

from __future__ import annotations

import os
from typing import Final


_REQUEST_LOGGING_ENV: Final[str] = "KISUKE_DEBUG"
_REQUEST_LOGGING_ENABLED: bool = os.getenv(_REQUEST_LOGGING_ENV, "0") == "1"


def is_enabled() -> bool:
    """Return True when request logging is currently allowed."""

    return _REQUEST_LOGGING_ENABLED


def set_enabled(value: bool) -> None:
    """Enable or disable request logging at runtime."""

    global _REQUEST_LOGGING_ENABLED
    _REQUEST_LOGGING_ENABLED = bool(value)


__all__ = ["is_enabled", "set_enabled"]
