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

            # Truncate system instructions (top-level Anthropic field)
            if "system" in log_payload:
                log_payload["system"] = "CODEX_INSTRUCTIONS_FULL"

            # Temporarily always show tools for debugging (no env toggle)
            show_tools = True
            if "tools" in log_payload and isinstance(log_payload.get("tools"), list):
                if not show_tools:
                    log_payload["tools"] = "ANTHROPIC_TOOL_LIST"
                else:
                    # Hide verbose fields to reduce log noise
                    try:
                        def _sanitize_schema(schema: Any) -> Any:
                            if not isinstance(schema, dict):
                                return schema
                            # Copy to avoid mutating original
                            s = {k: v for k, v in schema.items() if k not in {"$schema", "type", "title", "description", "additionalProperties", "items", "enum", "oneOf", "anyOf", "allOf", "definitions"}}
                            # Keep only property names without inner details
                            props = s.get("properties")
                            if isinstance(props, dict):
                                s["properties"] = {name: {} for name in props.keys()}
                            return s

                        sanitized_tools = []
                        for tool in log_payload["tools"]:
                            if isinstance(tool, dict):
                                t = dict(tool)
                                # Top-level Anthropic-style tool fields
                                t.pop("description", None)
                                if isinstance(t.get("input_schema"), dict):
                                    t["input_schema"] = _sanitize_schema(t["input_schema"])

                                # OpenAI function tool format
                                func = t.get("function")
                                if isinstance(func, dict):
                                    f = dict(func)
                                    f.pop("description", None)
                                    if isinstance(f.get("parameters"), dict):
                                        f["parameters"] = _sanitize_schema(f["parameters"])
                                    t["function"] = f

                                sanitized_tools.append(t)
                            else:
                                sanitized_tools.append(tool)
                        log_payload["tools"] = sanitized_tools
                    except Exception:
                        # If sanitization fails, fall back to original tools
                        pass

            # Suppress verbose system prompt inside messages[] by replacing content
            # of any system-role message with a compact placeholder. This avoids
            # logging long agent instructions like "You are a Claude agent, ...".
            if isinstance(log_payload.get("messages"), list):
                try:
                    new_msgs = []
                    for msg in log_payload["messages"]:
                        if isinstance(msg, dict) and (msg.get("role") == "system"):
                            sanitized = dict(msg)
                            sanitized["content"] = "ANTHROPIC_AGENT_PROMPT"
                            new_msgs.append(sanitized)
                        else:
                            new_msgs.append(msg)
                    log_payload["messages"] = new_msgs
                except Exception:
                    pass

            body_str = json.dumps(log_payload, indent=2, ensure_ascii=False)
            print(f"   Request Body (size: {len(json.dumps(payload, ensure_ascii=False))}):\n{body_str}")
        except Exception:
            print("   Request Body: <unserializable>")

    def _client_session(self) -> ClientSession:
        return ClientSession(timeout=self.timeout)


__all__ = ["ProviderExecutor"]
