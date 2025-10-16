"""Provider-specific system instruction helpers."""

from __future__ import annotations

from typing import Optional
from pathlib import Path
import json


def _load_instruction_text(candidates: list[str]) -> str:
    """Load instruction text.

    Supports files containing either a JSON string (escaped newlines) or raw text.
    Tries candidates in order and returns the first successfully loaded content.
    """
    for name in candidates:
        path = Path(__file__).with_name(name)
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            continue
        except Exception:
            continue

        # Try JSON-decoding first (handles files that store a JSON string)
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, str):
                return parsed
        except Exception:
            pass

        # Fallback: use raw text as-is
        return raw

    # If nothing found, return empty string
    return ""


# Unified instruction sources
GPT5_INSTRUCTIONS = _load_instruction_text(["gpt5_instructions.txt", "gpt5_instructions.text"])
CODEX_INSTRUCTIONS = _load_instruction_text(["gpt5_codex_instructions.txt"]) or GPT5_INSTRUCTIONS


def resolve_system_instruction(
    provider: str,
    auth_method: Optional[str],
    explicit_instruction: Optional[str],
    model: Optional[str] = None,
) -> Optional[str]:
    """Return a system instruction based on provider/auth/model.

    Rule:
      - If an explicit instruction is provided, return it.
      - If provider is Codex OAuth and model is "gpt-5-codex", use Codex instructions.
      - Otherwise, use the general GPT‑5 instructions.
    Notes:
      - Accepts provider values "codex" or "openai" for Codex backend flows.
    """
    if explicit_instruction:
        return explicit_instruction

    prov = (provider or "").lower()
    auth = (auth_method or "api_key").lower()
    mdl = (model or "").lower()

    if prov in {"codex", "openai"} and auth == "oauth" and mdl == "gpt-5-codex":
        return CODEX_INSTRUCTIONS

    return GPT5_INSTRUCTIONS


__all__ = [
    "GPT5_INSTRUCTIONS",
    "CODEX_INSTRUCTIONS",
    "resolve_system_instruction",
]
