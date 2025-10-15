"""Executor for Google Gemini API.

This executor handles direct communication with Google's Generative Language API,
using Gemini's native format (not OpenAI-compatible).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, Optional

from aiohttp import web
from aiohttp.client_exceptions import ClientConnectionError

from .base import ProviderExecutor
from ..auth import resolve_auth_strategy
from ..errors import anthropic_error_payload
from ..sse import sse_event
from ..translators.gemini import (
    anthropic_request_to_gemini,
    gemini_response_to_anthropic,
    gemini_response_to_anthropic_streaming,
)
from .. import logging_control


def debug_log(message: str, *args) -> None:
    """Debug logging helper."""
    if not logging_control.is_enabled():
        return
    formatted = message % args if args else message
    print(f"[DEBUG] {formatted}")


class GeminiExecutor(ProviderExecutor):
    """Handles native Gemini API communication."""

    # Gemini API constants
    GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com"
    API_VERSION = "v1beta"

    def __init__(self, cfg, request_body, requested_model, alt=None):
        super().__init__(cfg, request_body, requested_model, alt)
        self.effective_model = self._determine_model()

    def _determine_model(self) -> str:
        """Determine the effective model to use."""
        # Use requested model if provided, otherwise use config model
        model = self.requested_model or self.cfg.model

        # Ensure we have a valid Gemini model name
        if not model.startswith("gemini"):
            # Default to a Gemini model if not specified
            model = "gemini-1.5-flash"

        return model

    def _build_url(self, stream: bool = False) -> str:
        """Build Gemini API URL.

        For regular requests: /v1beta/models/{model}:generateContent
        For streaming: /v1beta/models/{model}:streamGenerateContent (or use alt=sse)
        """
        # Proxy is source of truth for base_url - always use hardcoded endpoint
        base = self.GEMINI_ENDPOINT.rstrip("/")

        # Determine the action based on streaming mode
        if stream and not self.alt:
            action = "streamGenerateContent"
        else:
            action = "generateContent"

        url = f"{base}/{self.API_VERSION}/models/{self.effective_model}:{action}"

        # For API key auth (not OAuth), add key as query parameter
        auth_method = (self.cfg.auth_method or "api_key").lower()
        if auth_method != "oauth" and self.cfg.api_key:
            url = f"{url}?key={self.cfg.api_key}"

        # Add alt parameter for SSE streaming per CLIProxyAPI spec
        # When alt="", add alt=sse for true SSE mode (only for streaming)
        # When alt has value, add $alt={value} for custom mode
        if self.alt == "" and action == "streamGenerateContent":
            connector = "&" if "?" in url else "?"
            url = f"{url}{connector}alt=sse"
        elif self.alt:
            connector = "&" if "?" in url else "?"
            url = f"{url}{connector}$alt={self.alt}"

        return url

    def _build_headers(self) -> Dict[str, str]:
        """Build request headers for Gemini."""
        headers = {
            "Content-Type": "application/json",
        }

        # Get auth headers from strategy
        auth = resolve_auth_strategy(self.cfg.provider, self.cfg)
        headers.update(auth.headers())

        # Add any extra headers from config (excluding reasoning which goes in body)
        extra_headers = dict(self.cfg.extra_headers or {})
        extra_headers.pop("reasoning", None)  # Remove reasoning from headers
        headers.update(extra_headers)

        return headers

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

    async def execute(self, request: web.Request) -> web.StreamResponse:
        """Execute request against Gemini API."""

        debug_log("GeminiExecutor: model=%s, alt=%s", self.effective_model, self.alt)

        # Extract reasoning level from extra_headers (if present)
        reasoning_level = None
        if self.cfg.extra_headers:
            reasoning_level = self.cfg.extra_headers.get("reasoning")

        # Convert Anthropic request to Gemini format
        try:
            gemini_body = anthropic_request_to_gemini(
                self.request_body,
                self.effective_model,
                system_instruction=self.cfg.system_instruction,
                reasoning_level=reasoning_level,
            )
        except Exception as e:
            debug_log("Failed to convert request: %s", str(e))
            return web.json_response(
                anthropic_error_payload(f"Request conversion failed: {str(e)}", "invalid_request_error"),
                status=400,
            )

        # Determine if we should stream
        stream = bool(self.request_body.get("stream", False))

        # Build URL and headers
        url = self._build_url(stream=stream)
        headers = self._build_headers()

        # Log the upstream request
        self._log_upstream(url, headers, gemini_body)

        debug_log("Gemini request to %s", url)
        debug_log("Request body keys: %s", list(gemini_body.keys()))

        try:
            async with self._client_session() as session:
                async with session.post(url, json=gemini_body, headers=headers) as upstream:
                    debug_log("Upstream response: status=%s", upstream.status)

                    if upstream.status >= 400:
                        error_body = await self._read_json(upstream)
                        debug_log("Upstream error: %s", error_body)

                        # Extract error message from Gemini format
                        error_msg = "Unknown error"
                        error_type = "api_error"

                        if isinstance(error_body, dict):
                            if "error" in error_body:
                                error_info = error_body["error"]
                                if isinstance(error_info, dict):
                                    error_msg = error_info.get("message", str(error_info))
                                    error_type = error_info.get("code", "api_error")
                                else:
                                    error_msg = str(error_info)
                            elif "message" in error_body:
                                error_msg = error_body["message"]

                        # Map Gemini error codes to Anthropic error types
                        error_type_map = {
                            "INVALID_ARGUMENT": "invalid_request_error",
                            "FAILED_PRECONDITION": "invalid_request_error",
                            "OUT_OF_RANGE": "invalid_request_error",
                            "UNAUTHENTICATED": "authentication_error",
                            "PERMISSION_DENIED": "permission_error",
                            "NOT_FOUND": "not_found_error",
                            "RESOURCE_EXHAUSTED": "rate_limit_error",
                            "INTERNAL": "api_error",
                            "UNAVAILABLE": "api_error",
                        }
                        error_type = error_type_map.get(error_type, "api_error")

                        if stream:
                            resp = web.StreamResponse(
                                status=upstream.status,
                                headers={"Content-Type": "text/event-stream"}
                            )
                            await resp.prepare(request)
                            await resp.write(f"event: error\ndata: {json.dumps(anthropic_error_payload(error_msg, error_type))}\n\n".encode())
                            await resp.write(b"event: message_stop\ndata: {\"type\": \"message_stop\"}\n\n")
                            await resp.write_eof()
                            return resp

                        return web.json_response(
                            anthropic_error_payload(error_msg, error_type),
                            status=upstream.status,
                        )

                    # Handle successful response
                    if stream or self.alt:
                        return await self._stream_response(request, upstream)
                    else:
                        return await self._non_stream_response(upstream)

        except ClientConnectionError as e:
            debug_log("Connection error: %s", str(e))
            error_msg = f"Failed to connect to Gemini API: {str(e)}"
            if stream:
                resp = web.StreamResponse(
                    status=502,
                    headers={"Content-Type": "text/event-stream"}
                )
                await resp.prepare(request)
                await resp.write(f"event: error\ndata: {json.dumps(anthropic_error_payload(error_msg, 'api_error'))}\n\n".encode())
                await resp.write(b"event: message_stop\ndata: {\"type\": \"message_stop\"}\n\n")
                await resp.write_eof()
                return resp
            return web.json_response(
                anthropic_error_payload(error_msg, "api_error"),
                status=502,
            )
        except Exception as e:
            debug_log("Unexpected error: %s", str(e))
            error_msg = f"Unexpected error: {str(e)}"
            if stream:
                resp = web.StreamResponse(
                    status=500,
                    headers={"Content-Type": "text/event-stream"}
                )
                await resp.prepare(request)
                await resp.write(f"event: error\ndata: {json.dumps(anthropic_error_payload(error_msg, 'api_error'))}\n\n".encode())
                await resp.write(b"event: message_stop\ndata: {\"type\": \"message_stop\"}\n\n")
                await resp.write_eof()
                return resp
            return web.json_response(
                anthropic_error_payload(error_msg, "api_error"),
                status=500,
            )

    async def _non_stream_response(self, upstream) -> web.Response:
        """Handle non-streaming response from Gemini."""
        try:
            gemini_response = await upstream.json()
            debug_log("Gemini response keys: %s", list(gemini_response.keys()) if isinstance(gemini_response, dict) else "not a dict")

            # Convert Gemini response to Anthropic format
            anthropic_response = gemini_response_to_anthropic(gemini_response)

            # Add metadata if present
            if self.metadata:
                anthropic_response["metadata"] = self.metadata

            return web.json_response(anthropic_response)

        except Exception as e:
            debug_log("Error processing response: %s", str(e))
            return web.json_response(
                anthropic_error_payload(f"Failed to process response: {str(e)}", "api_error"),
                status=500,
            )

    async def _stream_response(self, request: web.Request, upstream) -> web.StreamResponse:
        """Handle streaming response from Gemini."""
        resp = web.StreamResponse(
            headers={"Content-Type": "text/event-stream"}
        )
        await resp.prepare(request)

        try:
            # Context for streaming conversion
            conversion_context = {}

            # Read the streaming response
            async for line in upstream.content:
                if not line:
                    continue

                line_str = line.decode("utf-8").strip()
                if not line_str:
                    continue

                debug_log("Gemini stream line: %s", line_str[:100])

                # Convert Gemini SSE to Anthropic SSE events
                try:
                    events = gemini_response_to_anthropic_streaming(line_str, conversion_context)

                    for event in events:
                        event_type = event.get("event", "message")
                        event_data = event.get("data", {})

                        # Add metadata if this is a message_start event
                        if event_type == "message_start" and self.metadata:
                            event_data["message"]["metadata"] = self.metadata

                        # Write the SSE event
                        sse_line = f"event: {event_type}\ndata: {json.dumps(event_data)}\n\n"
                        await resp.write(sse_line.encode())

                except Exception as e:
                    debug_log("Error converting stream line: %s", str(e))
                    continue

            # Ensure we send message_stop if not already sent
            if not conversion_context.get("stop_sent"):
                await resp.write(b"event: message_stop\ndata: {\"type\": \"message_stop\"}\n\n")

        except Exception as e:
            debug_log("Stream error: %s", str(e))
            # Send error event
            await resp.write(f"event: error\ndata: {json.dumps(anthropic_error_payload(str(e), 'api_error'))}\n\n".encode())
            await resp.write(b"event: message_stop\ndata: {\"type\": \"message_stop\"}\n\n")

        await resp.write_eof()
        return resp

    async def count_tokens(self, request: web.Request) -> web.Response:
        """Count tokens for a request using Gemini API."""
        from ..translators.gemini import gemini_token_count_response

        debug_log("GeminiExecutor.count_tokens: model=%s", self.effective_model)

        # Extract reasoning level from extra_headers (if present)
        reasoning_level = None
        if self.cfg.extra_headers:
            reasoning_level = self.cfg.extra_headers.get("reasoning")

        # Convert Anthropic request to Gemini format
        try:
            gemini_body = anthropic_request_to_gemini(
                self.request_body,
                self.effective_model,
                system_instruction=self.cfg.system_instruction,
                reasoning_level=reasoning_level,
            )
        except Exception as e:
            debug_log("Failed to convert request: %s", str(e))
            return web.json_response(
                anthropic_error_payload(f"Request conversion failed: {str(e)}", "invalid_request_error"),
                status=400,
            )

        # For countTokens, remove tools and generationConfig (per CLIProxyAPI spec)
        gemini_body.pop("tools", None)
        gemini_body.pop("generationConfig", None)

        # Build URL for countTokens action
        url = f"{self.GEMINI_ENDPOINT}/{self.API_VERSION}/models/{self.effective_model}:countTokens"

        # Build headers
        headers = {"Content-Type": "application/json"}
        auth = resolve_auth_strategy(self.cfg.provider, self.cfg)
        headers.update(auth.headers())

        # Log the upstream request
        self._log_upstream(url, headers, gemini_body)

        debug_log("Gemini countTokens to %s", url)

        try:
            async with self._client_session() as session:
                async with session.post(url, json=gemini_body, headers=headers) as upstream:
                    debug_log("countTokens response: status=%s", upstream.status)

                    if upstream.status >= 400:
                        error_body = await upstream.text()
                        debug_log("countTokens error: %s", error_body)

                        # Try to parse as JSON for better error message
                        try:
                            error_json = json.loads(error_body)
                            error_msg = error_json.get("error", {}).get("message", error_body)
                        except:
                            error_msg = error_body or "Unknown error"

                        return web.json_response(
                            anthropic_error_payload(error_msg, "api_error"),
                            status=upstream.status,
                        )

                    # Success! Extract totalTokens
                    response_body = await upstream.json()
                    debug_log("countTokens response body: %s", response_body)

                    total_tokens = response_body.get("totalTokens", 0)

                    # Convert to Gemini format
                    gemini_response = gemini_token_count_response(total_tokens)

                    return web.json_response(gemini_response, status=200)

        except ClientConnectionError as e:
            debug_log("Connection error for countTokens: %s", str(e))
            return web.json_response(
                anthropic_error_payload(f"Failed to connect to Gemini API: {str(e)}", "api_error"),
                status=502,
            )
        except Exception as e:
            debug_log("Unexpected error for countTokens: %s", str(e))
            return web.json_response(
                anthropic_error_payload(f"Unexpected error: {str(e)}", "api_error"),
                status=500,
            )


__all__ = ["GeminiExecutor"]