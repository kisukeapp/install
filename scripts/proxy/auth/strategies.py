"""Authentication strategies used by providers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional

from ..config import ModelConfig


@dataclass
class AuthStrategy:
    """Base strategy that returns headers and optional query params."""

    token: Optional[str] = None

    def headers(self) -> Dict[str, str]:
        return {}

    def query_params(self) -> Dict[str, str]:
        return {}


@dataclass
class NullAuth(AuthStrategy):
    """Auth strategy for providers that manage authentication elsewhere."""

    def headers(self) -> Dict[str, str]:
        return {}


@dataclass
class BearerTokenAuth(AuthStrategy):
    """Attach a bearer token to the Authorization header."""

    header_name: str = "Authorization"
    prefix: str = "Bearer "

    def headers(self) -> Dict[str, str]:
        if not self.token:
            return {}
        return {self.header_name: f"{self.prefix}{self.token}"}


@dataclass
class ApiKeyHeaderAuth(AuthStrategy):
    """Attach a raw token to a specific header name."""

    header_name: str = "x-api-key"
    prefix: str = ""

    def headers(self) -> Dict[str, str]:
        if not self.token:
            return {}
        return {self.header_name: f"{self.prefix}{self.token}"}


@dataclass
class DualHeaderAuth(AuthStrategy):
    """Send both Bearer and x-api-key headers for maximum compatibility."""

    def headers(self) -> Dict[str, str]:
        if not self.token:
            return {}
        return {
            "Authorization": f"Bearer {self.token}",
            "x-api-key": self.token,
        }


def resolve_auth_strategy(provider: str, cfg: ModelConfig) -> AuthStrategy:
    """Return the appropriate auth strategy for a model configuration."""
    provider = provider.lower()
    method = (cfg.auth_method or "api_key").lower()
    token = cfg.api_key

    if provider == "anthropic":
        if method == "oauth":
            return BearerTokenAuth(token=token)
        return ApiKeyHeaderAuth(token=token, header_name="x-api-key")

    if provider == "azure":
        return ApiKeyHeaderAuth(token=token, header_name="api-key")

    if provider in {"gemini", "google"}:
        # Google/Gemini uses x-goog-api-key for API keys, Bearer for OAuth
        if method == "oauth":
            return BearerTokenAuth(token=token)
        return ApiKeyHeaderAuth(token=token, header_name="x-goog-api-key")

    if provider in {"openai", "openrouter", "ollama", "togetherai", "groq", "cerebras", "xai"}:
        return BearerTokenAuth(token=token)

    # Unknown provider - send both headers for maximum compatibility
    if method == "oauth":
        return BearerTokenAuth(token=token)

    if token:
        return DualHeaderAuth(token=token)

    return NullAuth()


__all__ = [
    "AuthStrategy",
    "BearerTokenAuth",
    "ApiKeyHeaderAuth",
    "DualHeaderAuth",
    "NullAuth",
    "resolve_auth_strategy",
]
