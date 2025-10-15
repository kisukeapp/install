"""Provider executor factory with protocol-based routing.

Following CLIProxyAPI's pattern:
- provider=='openai' -> Codex protocol (ChatGPT backend)
- provider=='anthropic' -> Native Anthropic API protocol
- provider=='google' -> Routes based on auth_method:
  - oauth -> Gemini CLI (Cloud Code Assist)
  - api_key -> Standard Gemini API
- provider=='gemini' -> Standard Gemini API
- Other providers -> OpenAI v1 API protocol
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from aiohttp import web

from ..config import ModelConfig
from .anthropic import AnthropicExecutor
from .base import ProviderExecutor
from .codex_executor import CodexExecutor
from .gemini_executor import GeminiExecutor
from .gemini_cli_executor import GeminiCLIExecutor
from .openai_v1_executor import OpenAIV1Executor


def get_executor(
    provider: str,
    cfg: ModelConfig,
    request_body: Dict[str, Any],
    requested_model: str,
    alt: Optional[str] = None,
) -> ProviderExecutor:
    """Get appropriate executor based on provider.

    Key routing logic:
    - provider == 'openai' -> CodexExecutor (ChatGPT backend protocol)
    - provider == 'anthropic' -> AnthropicExecutor (native)
    - provider == 'google' -> Routes based on auth_method:
        - oauth -> GeminiCLIExecutor (Cloud Code Assist)
        - api_key -> GeminiExecutor (standard Gemini API)
    - provider == 'gemini' -> GeminiExecutor (standard Gemini API)
    - All others -> OpenAIV1Executor (OpenAI-compatible API)
    """
    provider_key = provider.lower()

    # Route based on provider, NOT auth method
    if provider_key == "openai":
        # provider=='openai' ALWAYS means Codex protocol
        return CodexExecutor(cfg, request_body, requested_model)
    elif provider_key == "anthropic":
        # Native Anthropic
        return AnthropicExecutor(cfg, request_body, requested_model, alt=alt)
    elif provider_key == "google":
        # Google routes based on auth_method
        auth_method = (cfg.auth_method or "api_key").lower()
        if auth_method == "oauth":
            # OAuth → Gemini CLI (Cloud Code Assist)
            return GeminiCLIExecutor(cfg, request_body, requested_model, alt=alt)
        else:
            # API key → Standard Gemini API
            return GeminiExecutor(cfg, request_body, requested_model, alt=alt)
    elif provider_key == "gemini":
        # Direct Gemini specification uses standard API
        return GeminiExecutor(cfg, request_body, requested_model, alt=alt)
    elif provider_key in {
        "azure", "openrouter", "ollama", "custom",
        "togetherai", "groq", "cerebras", "xai",
    }:
        # All OpenAI-compatible providers use v1 API
        return OpenAIV1Executor(cfg, request_body, requested_model)
    else:
        # Unknown provider - try OpenAI v1 as fallback
        return OpenAIV1Executor(cfg, request_body, requested_model)


__all__ = ["get_executor"]
