"""Miscellaneous helpers used across proxy modules."""

from __future__ import annotations

from typing import Optional


def mask_secret(value: Optional[str]) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "****"
    return f"{value[:4]}...{value[-4:]}"


__all__ = ["mask_secret"]
