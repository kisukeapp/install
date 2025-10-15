"""Provider executor base classes."""

from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional

from aiohttp import ClientSession, ClientTimeout, web

from ..config import ModelConfig
from .. import logging_control
from ..metadata import ensure_metadata, extract_thinking
from ..utils import mask_secret


class ProviderExecutor:
    """Common base for provider executors."""

    def __init__(
        self,
        cfg: ModelConfig,
        request_body: Dict[str, Any],
        requested_model: str,
        alt: Optional[str] = None,
    ) -> None:
        self.cfg = cfg
        self.request_body = request_body
        self.requested_model = requested_model
        self.timeout = ClientTimeout(total=float(os.getenv("REQUEST_TIMEOUT", "120")))
        self.metadata = ensure_metadata(request_body if isinstance(request_body, dict) else None)
        self.thinking = extract_thinking(request_body if isinstance(request_body, dict) else None)
        self.alt = (alt or "").strip()

    async def execute(self, request: web.Request) -> web.StreamResponse:
        raise NotImplementedError

    def _log_upstream(self, url: str, headers: Dict[str, str], payload: Dict[str, Any]) -> None:
        if not logging_control.is_enabled():
            return
        masked_headers = {
            key: mask_secret(value) if "key" in key.lower() or "authorization" in key.lower() else value
            for key, value in headers.items()
        }
        print("\nUPSTREAM REQUEST:")
        print(f"   Provider: {self.cfg.provider}")
        print(f"   Model: {self.cfg.model}")
        print(f"   Auth Method: {getattr(self.cfg, 'auth_method', 'api_key')}")
        print(f"   URL: {url}")
        print(f"   Headers: {masked_headers}")
        try:
            # Create a truncated copy for logging
            log_payload = dict(payload)

            # Truncate system instructions
            if "system" in log_payload:
                log_payload["system"] = "CODEX_INSTRUCTIONS_FULL"

            # Truncate tools array
            if "tools" in log_payload and isinstance(log_payload["tools"], list):
                log_payload["tools"] = "ANTHROPIC_TOOL_LIST"

            body_str = json.dumps(log_payload, indent=2, ensure_ascii=False)
            print(f"   Request Body (size: {len(json.dumps(payload, ensure_ascii=False))}):\n{body_str}")
        except Exception:
            print("   Request Body: <unserializable>")

    def _client_session(self) -> ClientSession:
        return ClientSession(timeout=self.timeout)


__all__ = ["ProviderExecutor"]
