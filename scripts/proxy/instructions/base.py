"""Provider-specific system instruction helpers."""

from __future__ import annotations

from typing import Dict, Optional, Tuple
from pathlib import Path
import json

CODEX_INSTRUCTIONS = json.loads(Path(__file__).with_name("gpt5_codex_instructions.txt").read_text(encoding="utf-8"))

_DEFAULTS: Dict[Tuple[str, str], str] = {
    ("openai", "oauth"): CODEX_INSTRUCTIONS,
}


def resolve_system_instruction(
    provider: str,
    auth_method: Optional[str],
    explicit_instruction: Optional[str],
) -> Optional[str]:
    """Return a system instruction for the given provider/auth combination."""
    if explicit_instruction:
        return explicit_instruction
    key = (provider.lower(), (auth_method or "api_key").lower())
    return _DEFAULTS.get(key)


__all__ = ["CODEX_INSTRUCTIONS", "resolve_system_instruction"]
