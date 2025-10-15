"""Model routing helpers for mapping Anthropic model names to size classes."""

from __future__ import annotations

from typing import Literal

ModelSize = Literal["small", "medium", "big"]


def get_model_size(anthropic_model: str) -> ModelSize:
    """Infer model size category from an Anthropic model identifier."""
    model = (anthropic_model or "").lower()
    if "haiku" in model:
        return "small"
    if "sonnet" in model:
        return "medium"
    if "opus" in model:
        return "big"
    return "medium"


def default_openai_model(anthropic_model: str) -> str:
    """Fallback mapping for common Claude families to OpenAI-compatible models."""
    model = (anthropic_model or "").lower()
    if "haiku" in model:
        return "gpt-4o-mini"
    if "sonnet" in model:
        return "gpt-4o"
    if "opus" in model:
        return "gpt-4o"
    return "gpt-4o"


__all__ = ["ModelSize", "get_model_size", "default_openai_model"]
