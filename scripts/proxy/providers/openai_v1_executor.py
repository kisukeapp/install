"""Executor for OpenAI v1 API protocol.

This executor handles all OpenAI-compatible providers EXCEPT provider=="openai".
Examples: openrouter, groq, azure, ollama, etc.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any, Dict

from aiohttp import web
from aiohttp.client_exceptions import ClientConnectionError

from .base import ProviderExecutor
from .. import logging_control
from ..auth import resolve_auth_strategy
from ..context import TranslationContext
from ..errors import anthropic_error_payload
from ..sse import sse_event, iter_openai_sse
from ..translators.openai_v1 import (
    anthropic_request_to_openai_v1,
    openai_v1_response_to_anthropic_streaming,
    openai_v1_response_to_anthropic,
)


def debug_log(message: str, *args) -> None:
    """Debug logging helper."""
    if not logging_control.is_enabled():
        return
    formatted = message % args if args else message
    print(f"[DEBUG] {formatted}")


class OpenAIV1Executor(ProviderExecutor):
    """Handles OpenAI v1 API protocol for OpenAI-compatible providers."""

    def __init__(self, cfg, request_body, requested_model, alt=None):
        super().__init__(cfg, request_body, requested_model, alt)
        self.context = self._create_context()

    def _create_context(self) -> TranslationContext:
        """Create context for OpenAI v1 translation."""
        return TranslationContext.for_openai_v1(self.requested_model or self.cfg.model)

    async def _read_json(self, response) -> Dict[str, Any]:
        """Read JSON from response with fallback."""
        try:
            return await response.json()
        except Exception:
            try:
                text = await response.text()
                try:
                    parsed = json.loads(text)
                    if isinstance(parsed, dict):
                        return parsed
                except Exception:
                    pass
                return {"message": text}
            except Exception:
                return {"message": "unknown upstream error"}

    def _build_url(self) -> str:
        """Build OpenAI v1 API URL."""
        base = self.cfg.base_url.rstrip("/")

        # Handle Azure-specific URL format
        if self.cfg.provider == "azure":
            if self.cfg.azure_deployment and self.cfg.azure_api_version:
                return f"{base}/openai/deployments/{self.cfg.azure_deployment}/chat/completions?api-version={self.cfg.azure_api_version}"

        # Standard OpenAI v1 format
        return f"{base}/chat/completions"

    def _build_headers(self, auth_headers: Dict[str, str]) -> Dict[str, str]:
        """Build request headers for OpenAI v1."""
        headers = {
            "Content-Type": "application/json",
        }

        # Add auth headers
        headers.update(auth_headers)

        # Add any extra headers (excluding reasoning which goes in body)
        extra_headers = dict(self.cfg.extra_headers or {})
        extra_headers.pop("reasoning", None)  # Remove reasoning from headers
        headers.update(extra_headers)

        return headers

    async def execute(self, request: web.Request) -> web.StreamResponse:
        """Execute request against OpenAI v1 API."""

        # Translate request
        upstream_body = anthropic_request_to_openai_v1(
            self.request_body,
            self.context,
        )

        # Override model if configured (for OpenAI v1, models are typically used as-is)
        if self.cfg.model:
            upstream_body["model"] = self.cfg.model
            self.context.effective_model = self.cfg.model

        # Extract reasoning level from extra_headers and add to request body
        if self.cfg.extra_headers:
            reasoning_level = self.cfg.extra_headers.get("reasoning")
            if reasoning_level and reasoning_level.lower() in ["low", "medium", "high"]:
                upstream_body["reasoning_effort"] = reasoning_level.lower()

        # Build request
        url = self._build_url()
        auth = resolve_auth_strategy(self.cfg.provider, self.cfg)
        headers = self._build_headers(auth.headers())

        self._log_upstream(url, headers, upstream_body)

        stream = bool(self.request_body.get("stream", False))

        try:
            async with self._client_session() as session:
                async with session.post(url, json=upstream_body, headers=headers) as upstream:
                    debug_log("Upstream response: status=%s", upstream.status)

                    if upstream.status >= 400:
                        error_body = await self._read_json(upstream)
                        error_type = error_body.get("error", {}).get("type", "api_error") if isinstance(
                            error_body.get("error"), dict
                        ) else "api_error"
                        message = error_body.get("error", {}).get("message", str(error_body)) if isinstance(
                            error_body.get("error"), dict
                        ) else str(error_body)

                        debug_log("Upstream error: status=%s type=%s message=%s", upstream.status, error_type, message)

                        if stream:
                            resp = web.StreamResponse(
                                status=upstream.status,
                                headers={"Content-Type": "text/event-stream"}
                            )
                            await resp.prepare(request)
                            await resp.write(f"event: error\ndata: {json.dumps(anthropic_error_payload(message, error_type))}\n\n".encode())
                            await resp.write(b"event: message_stop\ndata: {\"type\": \"message_stop\"}\n\n")
                            await resp.write_eof()
                            return resp

                        return web.json_response(
                            anthropic_error_payload(message, error_type),
                            status=upstream.status,
                        )

                    if stream:
                        return await self._stream_response(request, upstream)
                    return await self._non_stream_response(upstream)

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            debug_log("OpenAI v1 executor error: %s", exc)
            return web.json_response(
                anthropic_error_payload(f"Upstream error: {exc}"),
                status=502,
            )

    async def _stream_response(self, request: web.Request, upstream) -> web.StreamResponse:
        """Stream OpenAI v1 SSE to Anthropic SSE."""
        resp = web.StreamResponse(
            status=200,
            headers={"Content-Type": "text/event-stream"}
        )
        await resp.prepare(request)

        # Reset streaming context
        self.context.reset_streaming()

        try:
            async for chunk in iter_openai_sse(upstream):
                # Log tool calls if present
                choices = chunk.get("choices", [])
                if choices:
                    choice = choices[0]
                    delta = choice.get("delta", {})
                    tool_calls = delta.get("tool_calls")
                    if tool_calls:
                        debug_log("Received OpenAI tool_calls delta: %s", tool_calls)

                # Translate OpenAI chunk to Anthropic events
                anthropic_events = openai_v1_response_to_anthropic_streaming(
                    chunk,
                    self.context
                )

                # Send Anthropic events
                for event_type, data in anthropic_events:
                    await resp.write(sse_event(event_type, data))

        except (ConnectionResetError, ClientConnectionError) as exc:
            debug_log("Client disconnected during OpenAI v1 streaming: %s", exc)

        return resp

    async def _non_stream_response(self, upstream) -> web.StreamResponse:
        """Convert OpenAI v1 response to Anthropic format."""
        payload = await upstream.json()

        # Log tool calls if present
        choices = payload.get("choices", [])
        if choices:
            message = choices[0].get("message", {})
            tool_calls = message.get("tool_calls")
            if tool_calls:
                debug_log("Non-stream received %d tool calls from OpenAI", len(tool_calls))
                for tc in tool_calls:
                    function = tc.get("function", {})
                    debug_log(
                        "Converting tool call: OpenAI[id=%s, name=%s]",
                        tc.get("id"),
                        function.get("name")
                    )

        # Convert to Anthropic format
        anthropic_response = openai_v1_response_to_anthropic(
            payload,
            self.context
        )

        return web.json_response(anthropic_response)