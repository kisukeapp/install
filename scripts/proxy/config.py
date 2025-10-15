"""Configuration dataclasses for proxy routing and provider setup."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional


@dataclass
class ModelConfig:
    """Provider configuration attached to a single route token."""

    provider: str = "openai"
    base_url: str = "https://api.openai.com/v1"
    api_key: str = ""
    model: str = "gpt-4o"
    extra_headers: Dict[str, str] = field(default_factory=dict)
    azure_deployment: Optional[str] = None
    azure_api_version: Optional[str] = None
    auth_method: Optional[str] = None
    system_instruction: Optional[str] = None


__all__ = ["ModelConfig"]
