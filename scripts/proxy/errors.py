"""Error normalization utilities."""

from __future__ import annotations

import json
from typing import Any, Dict, Tuple


def extract_error_details(err: Dict[str, Any]) -> Tuple[str, str]:
    """Return Anthropic-compatible (type, message) from an upstream error payload."""
    message = err.get("message", str(err))
    error_type = err.get("type", "api_error")

    if isinstance(message, str) and message.startswith("{"):
        try:
            nested = json.loads(message)
            if isinstance(nested, dict) and "error" in nested:
                details = nested["error"]
                if isinstance(details, dict):
                    message = details.get("message", message)
                    error_type = details.get("type", error_type)
        except Exception:
            pass

    return error_type, message


def anthropic_error_payload(message: str, error_type: str = "api_error") -> Dict[str, Any]:
    return {"type": "error", "error": {"type": error_type, "message": message}}


__all__ = ["extract_error_details", "anthropic_error_payload"]
