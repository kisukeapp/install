"""Executor for Google Gemini CLI/Cloud Code Assist API.

This executor handles communication with Google's Cloud Code Assist API,
which uses OAuth authentication and the v1internal endpoint.
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional

from aiohttp import web
from aiohttp.client_exceptions import ClientConnectionError

from .base import ProviderExecutor
from ..auth import resolve_auth_strategy
from ..errors import anthropic_error_payload
from ..sse import sse_event
from ..translators.gemini_cli import (
    anthropic_request_to_gemini_cli,
    gemini_cli_response_to_anthropic,
    gemini_cli_response_to_anthropic_streaming,
)
from .. import logging_control


def debug_log(message: str, *args) -> None:
    """Debug logging helper."""
    if not logging_control.is_enabled():
        return
    formatted = message % args if args else message
    print(f"[DEBUG] {formatted}")


class GeminiCLIExecutor(ProviderExecutor):
    """Handles Gemini CLI/Cloud Code Assist API communication."""

    # Cloud Code Assist API constants
    CODEASSIST_ENDPOINT = "https://cloudcode-pa.googleapis.com"
    API_VERSION = "v1internal"

    def __init__(self, cfg, request_body, requested_model, alt=None):
        super().__init__(cfg, request_body, requested_model, alt)
        self.effective_model = self._determine_model()
        self.project_id = self._extract_project_id()

    def _determine_model(self) -> str:
        """Determine the effective model to use."""
        # Use requested model if provided, otherwise use config model
        model = self.requested_model or self.cfg.model

        # Ensure we have a valid Gemini model name
        if not model.startswith("gemini"):
            # Default to a Gemini model if not specified
            model = "gemini-2.5-flash"

        return model

    def _get_model_fallback_order(self, base_model: str) -> list:
        """Get model fallback order with preview models first."""
        fallback_map = {
            "gemini-2.5-pro": [
                "gemini-2.5-pro-preview-05-06",
                "gemini-2.5-pro-preview-06-05",
                "gemini-2.5-pro"
            ],
            "gemini-2.5-flash": [
                "gemini-2.5-flash-preview-04-17",
                "gemini-2.5-flash-preview-05-20",
                "gemini-2.5-flash"
            ],
            "gemini-2.5-flash-lite": [
                "gemini-2.5-flash-lite-preview-06-17",
                "gemini-2.5-flash-lite"
            ],
        }

        # Return the fallback order, or just the base model if no fallbacks defined
        return fallback_map.get(base_model, [base_model])

    def _extract_project_id(self) -> Optional[str]:
        """Extract project ID from config."""
        # Check extra headers for project_id (temporary way to pass it for testing)
        if self.cfg.extra_headers:
            return self.cfg.extra_headers.get("project_id")
        return None

    def _extract_reasoning_level(self) -> Optional[str]:
        """Extract reasoning level from config."""
        # Check extra headers for reasoning level
        if self.cfg.extra_headers:
            return self.cfg.extra_headers.get("reasoning")
        return None

    def _build_url(self, action: str = "generateContent") -> str:
        """Build Gemini CLI API URL.

        Actions include:
        - generateContent
        - streamGenerateContent
        - loadCodeAssist
        - onboardUser
        - countTokens
        """
        # Proxy is source of truth for base_url - always use hardcoded endpoint
        base = self.CODEASSIST_ENDPOINT.rstrip("/")

        url = f"{base}/{self.API_VERSION}:{action}"

        # Add alt parameter for SSE streaming per CLIProxyAPI spec
        # When alt="", add ?alt=sse for true SSE mode
        # When alt has value, add ?$alt={value} for custom mode
        if self.alt == "" and action == "streamGenerateContent":
            url += "?alt=sse"
        elif self.alt:
            url += f"?$alt={self.alt}"

        return url

    def _build_headers(self, stream: bool = False) -> Dict[str, str]:
        """Build request headers for Gemini CLI."""
        headers = {
            "Content-Type": "application/json",
            # Required headers for Gemini CLI (from CLIProxyAPI)
            "User-Agent": "google-api-nodejs-client/9.15.1",
            "X-Goog-Api-Client": "gl-node/22.17.0",
            "Client-Metadata": "ideType=IDE_UNSPECIFIED,platform=PLATFORM_UNSPECIFIED,pluginType=GEMINI",
        }

        # Set appropriate Accept header based on streaming mode
        if stream:
            headers["Accept"] = "text/event-stream"
        else:
            headers["Accept"] = "application/json"

        # Get auth headers from strategy (should be Bearer token for OAuth)
        auth = resolve_auth_strategy(self.cfg.provider, self.cfg)
        headers.update(auth.headers())

        # Add any extra headers from config (but don't let project_id or reasoning leak as headers)
        extra_headers = dict(self.cfg.extra_headers or {})
        extra_headers.pop("project_id", None)  # Remove project_id from headers
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
        """Execute request against Gemini CLI API with model fallback retry."""

        debug_log("GeminiCLIExecutor: model=%s, project_id=%s", self.effective_model, self.project_id)

        if not self.project_id:
            debug_log("WARNING: No project_id found - Cloud Code Assist requires this!")
            return web.json_response(
                anthropic_error_payload("Missing project_id for Cloud Code Assist", "invalid_request_error"),
                status=400,
            )

        # Determine the action based on request metadata or streaming
        stream = bool(self.request_body.get("stream", False))
        action = "generateContent"

        # Check for special actions in metadata
        if self.metadata and "action" in self.metadata:
            action = self.metadata["action"]
        elif stream and not self.alt:
            action = "streamGenerateContent"

        # Extract reasoning level
        reasoning_level = self._extract_reasoning_level()

        # Convert Anthropic request to Gemini CLI format (base translation)
        try:
            base_gemini_body = anthropic_request_to_gemini_cli(
                self.request_body,
                self.effective_model,
                system_instruction=self.cfg.system_instruction,
                project_id=self.project_id,
                reasoning_level=reasoning_level,
            )
        except Exception as e:
            debug_log("Failed to convert request: %s", str(e))
            return web.json_response(
                anthropic_error_payload(f"Request conversion failed: {str(e)}", "invalid_request_error"),
                status=400,
            )

        # Debug dump if enabled
        debug_dump = os.environ.get("KISUKE_DEBUG_DUMP")
        if debug_dump:
            timestamp = int(time.time() * 1000)
            debug_dir = Path(debug_dump)
            try:
                debug_dir.mkdir(parents=True, exist_ok=True)
                with open(debug_dir / f"anthropic_{timestamp}.json", "w", encoding="utf-8") as fh:
                    json.dump(self.request_body, fh, indent=2, ensure_ascii=False)
                with open(debug_dir / f"gemini_cli_{timestamp}.json", "w", encoding="utf-8") as fh:
                    json.dump(base_gemini_body, fh, indent=2, ensure_ascii=False)
                debug_log("Debug dump written to %s", debug_dir)
            except Exception as dump_exc:
                debug_log("Failed to write debug dump: %s", str(dump_exc))

        # Get model fallback order
        models = self._get_model_fallback_order(self.effective_model)
        debug_log("Model fallback order: %s", models)

        # Track last error for retries
        last_status = 0
        last_error_msg = "Unknown error"
        last_error_type = "api_error"

        # Try each model in fallback order
        for attempt_model in models:
            # Clone and update gemini_body for this attempt
            gemini_body = copy.deepcopy(base_gemini_body)
            gemini_body["model"] = attempt_model

            # Build URL and headers for this attempt
            url = self._build_url(action)
            headers = self._build_headers(stream=stream)

            # Log the upstream request
            self._log_upstream(url, headers, gemini_body)

            debug_log("Gemini CLI request to %s with model %s", url, attempt_model)
            debug_log("Request body keys: %s", list(gemini_body.keys()))

            try:
                async with self._client_session() as session:
                    async with session.post(url, json=gemini_body, headers=headers) as upstream:
                        debug_log("Upstream response: status=%s for model %s", upstream.status, attempt_model)

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

                            # Store error for potential final return
                            last_status = upstream.status
                            last_error_msg = error_msg
                            last_error_type = error_type

                            # On 429 (rate limit), try next model
                            if upstream.status == 429:
                                debug_log("Got 429 for model %s, trying next model", attempt_model)
                                continue

                            # For other errors, break and return immediately
                            if stream or self.alt:
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

                        # Success! Handle response
                        if stream or self.alt:
                            return await self._stream_response(request, upstream)
                        else:
                            return await self._non_stream_response(upstream)

            except ClientConnectionError as e:
                debug_log("Connection error for model %s: %s", attempt_model, str(e))
                # Connection errors are not retry-able, return immediately
                error_msg = f"Failed to connect to Gemini CLI API: {str(e)}"
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
                debug_log("Unexpected error for model %s: %s", attempt_model, str(e))
                # Unexpected errors are not retry-able, return immediately
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

        # All models exhausted without success, return last error
        debug_log("All models exhausted, returning last error: %s", last_error_msg)
        if last_status == 0:
            last_status = 429  # Default to 429 if no status recorded

        if stream or self.alt:
            resp = web.StreamResponse(
                status=last_status,
                headers={"Content-Type": "text/event-stream"}
            )
            await resp.prepare(request)
            await resp.write(f"event: error\ndata: {json.dumps(anthropic_error_payload(last_error_msg, last_error_type))}\n\n".encode())
            await resp.write(b"event: message_stop\ndata: {\"type\": \"message_stop\"}\n\n")
            await resp.write_eof()
            return resp

        return web.json_response(
            anthropic_error_payload(last_error_msg, last_error_type),
            status=last_status,
        )

    async def _non_stream_response(self, upstream) -> web.Response:
        """Handle non-streaming response from Gemini CLI."""
        try:
            gemini_response = await upstream.json()
            debug_log("Gemini CLI response keys: %s", list(gemini_response.keys()) if isinstance(gemini_response, dict) else "not a dict")

            # Convert Gemini response to Anthropic format
            anthropic_response = gemini_cli_response_to_anthropic(gemini_response)

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
        """Handle streaming response from Gemini CLI."""
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

                debug_log("Gemini CLI stream line: %s", line_str[:100])

                # Convert Gemini SSE to Anthropic SSE events
                try:
                    events = gemini_cli_response_to_anthropic_streaming(line_str, conversion_context)

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
        """Count tokens for a request using Gemini CLI API with model fallback."""
        from ..translators.gemini_cli import gemini_cli_token_count_response

        debug_log("GeminiCLIExecutor.count_tokens: model=%s", self.effective_model)

        if not self.project_id:
            debug_log("WARNING: No project_id found - Cloud Code Assist requires this!")
            return web.json_response(
                anthropic_error_payload("Missing project_id for Cloud Code Assist", "invalid_request_error"),
                status=400,
            )

        # Extract reasoning level
        reasoning_level = self._extract_reasoning_level()

        # Convert Anthropic request to Gemini CLI format (base translation)
        try:
            base_gemini_body = anthropic_request_to_gemini_cli(
                self.request_body,
                self.effective_model,
                system_instruction=self.cfg.system_instruction,
                project_id=self.project_id,
                reasoning_level=reasoning_level,
            )
        except Exception as e:
            debug_log("Failed to convert request: %s", str(e))
            return web.json_response(
                anthropic_error_payload(f"Request conversion failed: {str(e)}", "invalid_request_error"),
                status=400,
            )

        # Get model fallback order
        models = self._get_model_fallback_order(self.effective_model)
        debug_log("Model fallback order for countTokens: %s", models)

        # Track last error for retries
        last_status = 0
        last_error_msg = "Unknown error"
        last_error_type = "api_error"

        # Try each model in fallback order
        for attempt_model in models:
            # Clone gemini_body for this attempt
            gemini_body = copy.deepcopy(base_gemini_body)

            # For countTokens, DELETE project and model fields (per CLIProxyAPI spec)
            gemini_body.pop("project", None)
            gemini_body.pop("model", None)

            # Build URL and headers for countTokens
            url = self._build_url("countTokens")
            headers = self._build_headers(stream=False)

            # Log the upstream request
            self._log_upstream(url, headers, gemini_body)

            debug_log("Gemini CLI countTokens to %s with model %s", url, attempt_model)

            try:
                async with self._client_session() as session:
                    async with session.post(url, json=gemini_body, headers=headers) as upstream:
                        debug_log("countTokens response: status=%s for model %s", upstream.status, attempt_model)

                        if upstream.status >= 400:
                            error_body = await self._read_json(upstream)
                            debug_log("countTokens error: %s", error_body)

                            # Extract error message
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

                            # Map error codes
                            error_type_map = {
                                "INVALID_ARGUMENT": "invalid_request_error",
                                "UNAUTHENTICATED": "authentication_error",
                                "PERMISSION_DENIED": "permission_error",
                                "RESOURCE_EXHAUSTED": "rate_limit_error",
                                "INTERNAL": "api_error",
                            }
                            error_type = error_type_map.get(error_type, "api_error")

                            # Store error for potential final return
                            last_status = upstream.status
                            last_error_msg = error_msg
                            last_error_type = error_type

                            # On 429, try next model
                            if upstream.status == 429:
                                debug_log("Got 429 for countTokens with model %s, trying next", attempt_model)
                                continue

                            # For other errors, return immediately
                            return web.json_response(
                                anthropic_error_payload(error_msg, error_type),
                                status=upstream.status,
                            )

                        # Success! Extract totalTokens
                        response_body = await self._read_json(upstream)
                        debug_log("countTokens response body: %s", response_body)

                        total_tokens = response_body.get("totalTokens", 0)

                        # Convert to Gemini format
                        gemini_response = gemini_cli_token_count_response(total_tokens)

                        return web.json_response(gemini_response, status=200)

            except ClientConnectionError as e:
                debug_log("Connection error for countTokens: %s", str(e))
                return web.json_response(
                    anthropic_error_payload(f"Failed to connect to Gemini CLI API: {str(e)}", "api_error"),
                    status=502,
                )
            except Exception as e:
                debug_log("Unexpected error for countTokens: %s", str(e))
                return web.json_response(
                    anthropic_error_payload(f"Unexpected error: {str(e)}", "api_error"),
                    status=500,
                )

        # All models exhausted
        debug_log("All models exhausted for countTokens, returning last error")
        if last_status == 0:
            last_status = 429

        return web.json_response(
            anthropic_error_payload(last_error_msg, last_error_type),
            status=last_status,
        )


__all__ = ["GeminiCLIExecutor"]