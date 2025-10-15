"""Executor for Codex protocol (ChatGPT backend).

This executor handles provider=="openai" which uses the ChatGPT backend API,
not the standard OpenAI v1 API.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any, Dict, Optional

from aiohttp import web
from aiohttp.client_exceptions import ClientConnectionError

from .base import ProviderExecutor
from ..auth import resolve_auth_strategy
from ..context import TranslationContext
from ..errors import anthropic_error_payload
from ..sse import sse_event, new_message_stub
from .. import logging_control
from ..translators.codex import (
    anthropic_request_to_codex,
    codex_response_to_anthropic_streaming,
)


def debug_log(message: str, *args) -> None:
    """Debug logging helper."""
    if not logging_control.is_enabled():
        return
    formatted = message % args if args else message
    print(f"[DEBUG] {formatted}")


class CodexExecutor(ProviderExecutor):
    """Handles Codex protocol for provider=='openai'."""

    # ChatGPT backend endpoint - proxy is source of truth
    CHATGPT_ENDPOINT = "https://chatgpt.com"

    def __init__(self, cfg, request_body, requested_model, alt=None):
        super().__init__(cfg, request_body, requested_model, alt)
        self.context = self._create_context()

    def _create_context(self) -> TranslationContext:
        """Create context for Codex translation."""
        return TranslationContext.for_codex(self.requested_model or self.cfg.model)

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
        """Build Codex API URL."""
        # Proxy is source of truth for base_url - always use hardcoded endpoint
        return f"{self.CHATGPT_ENDPOINT.rstrip('/')}/backend-api/codex/responses"

    def _build_headers(self, auth_headers: Dict[str, str]) -> Dict[str, str]:
        """Build request headers for Codex."""
        headers = {
            "Content-Type": "application/json",
            "Version": "0.21.0",
            "Openai-Beta": "responses=experimental",
            "Session_id": str(uuid.uuid4()),
            "Accept": "text/event-stream",
            "Connection": "Keep-Alive",
        }

        # Add originator for non-API key auth
        auth_method = (self.cfg.auth_method or "oauth").lower()
        if auth_method != "api_key":
            headers["Originator"] = "codex_cli_rs"

        # Add auth headers
        headers.update(auth_headers)

        # Add any extra headers (excluding reasoning which goes in body)
        extra_headers = dict(self.cfg.extra_headers or {})
        extra_headers.pop("reasoning", None)  # Remove reasoning from headers
        headers.update(extra_headers)

        return headers

    async def execute(self, request: web.Request) -> web.StreamResponse:
        """Execute request against Codex API."""

        debug_log("CodexExecutor: requested_model=%s, cfg.model=%s", self.requested_model, self.cfg.model)

        # Extract reasoning level from extra_headers (if present)
        reasoning_level = None
        if self.cfg.extra_headers:
            reasoning_level = self.cfg.extra_headers.get("reasoning")

        # Translate request
        upstream_body = anthropic_request_to_codex(
            self.request_body,
            self.context,
            provider=self.cfg.provider,
            auth_method=self.cfg.auth_method or "oauth",
            explicit_instruction=self.cfg.system_instruction,
            reasoning_level=reasoning_level,
        )

        debug_log("Codex request body model=%s", upstream_body.get("model"))
        debug_log("Codex request keys=%s", list(upstream_body.keys()))

        # Log the full request in debug mode for troubleshooting
        import json
        debug_log("Full Codex request:\n%s", json.dumps(upstream_body, indent=2)[:1000])

        # Note: We do NOT override the model here because the translator
        # has already mapped it to the correct base model (e.g., gpt-5-codex-medium -> gpt-5-codex)
        # The config model is used for context tracking but not for the actual API call

        # Force streaming for Codex
        upstream_body["stream"] = True

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
            debug_log("Codex executor error: %s", exc)
            return web.json_response(
                anthropic_error_payload(f"Upstream error: {exc}"),
                status=502,
            )

    async def _stream_response(self, request: web.Request, upstream) -> web.StreamResponse:
        """Stream Codex SSE to Anthropic SSE."""
        resp = web.StreamResponse(
            status=200,
            headers={"Content-Type": "text/event-stream"}
        )
        await resp.prepare(request)

        # Reset streaming context
        self.context.reset_streaming()

        try:
            # Process SSE line by line exactly like CLIProxyAPI bufio.Scanner
            buffer = b""

            async for chunk in upstream.content.iter_any():
                if not chunk:
                    continue
                buffer += chunk

                # Process each line immediately like CLIProxyAPI scanner.Scan()
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)

                    # Only process data: lines like CLIProxyAPI does
                    if line.startswith(b"data:"):
                        data_str = line[5:].strip()
                        if data_str and data_str != b"[DONE]":
                            try:
                                event_data = json.loads(data_str.decode("utf-8", errors="ignore"))
                                event_name = event_data.get("type", "")

                                if event_name:
                                    # Debug log for response.completed
                                    if event_name == "response.completed":
                                        debug_log("response.completed raw event_data: %s", json.dumps(event_data, indent=2)[:500])

                                    # Translate Codex event to Anthropic events immediately
                                    # Returns fully framed SSE bytes, not tuples
                                    anthropic_frames = codex_response_to_anthropic_streaming(
                                        event_name,
                                        event_data,
                                        self.context
                                    )

                                    # Send Anthropic events immediately (already fully framed bytes)
                                    for frame in anthropic_frames:
                                        await resp.write(frame)
                            except Exception as e:
                                debug_log("Error parsing data line: %s", e)
                    # Ignore event: lines and empty lines like CLIProxyAPI does

            # Properly close the stream
            await resp.write_eof()

        except (ConnectionResetError, ClientConnectionError) as exc:
            debug_log("Client disconnected during Codex streaming: %s", exc)

        return resp

    async def _non_stream_response(self, upstream) -> web.StreamResponse:
        """Accumulate Codex SSE and return a single Anthropic JSON response.

        Mirrors CLIProxyAPI: we scan the SSE and find the single 'response.completed' payload,
        then synthesize the final Claude message from it (no SSE framing here).
        """
        # Reset any prior streaming state
        self.context.reset_streaming()

        # Read entire upstream body (Codex still streams lines even for non-stream mode)
        try:
            raw = await upstream.read()
        except Exception as exc:
            return web.json_response(
                anthropic_error_payload(f"Upstream read error: {exc}"),
                status=502,
            )

        # Find the response.completed event payload
        completed_payload = None
        for raw_line in raw.split(b"\n"):
            if not raw_line.startswith(b"data:"):
                continue
            data_str = raw_line[5:].strip()
            if not data_str or data_str == b"[DONE]":
                continue
            try:
                evt = json.loads(data_str.decode("utf-8", errors="ignore"))
            except Exception:
                continue
            if isinstance(evt, dict) and evt.get("type") == "response.completed":
                completed_payload = evt
                break

        if not completed_payload:
            return web.json_response(
                anthropic_error_payload(
                    "stream error: disconnected before completion (missing response.completed)"
                ),
                status=408,
            )

        # Build final Anthropic message from the response.completed object
        message = self._build_message_from_completed(completed_payload)

        return web.json_response(message)

    def _build_message_from_completed(self, completed: Dict[str, Any]) -> Dict[str, Any]:
        """Reconstruct Anthropic message JSON from Codex response.completed event."""
        response = completed.get("response", {}) or {}

        # Base message
        message = {
            "id": response.get("id") or f"msg_{uuid.uuid4().hex}",
            "type": "message",
            "role": "assistant",
            "model": response.get("model") or self.requested_model or self.cfg.model,
            "content": [],
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {
                "input_tokens": int(response.get("usage", {}).get("input_tokens", 0) or 0),
                "output_tokens": int(response.get("usage", {}).get("output_tokens", 0) or 0),
            },
        }

        content_blocks = []
        has_tool_call = False

        # Retrieve short->original name map from context
        from ..translators.codex import _get_tool_name_maps
        maps = _get_tool_name_maps(self.context)
        short_to_orig = maps.get("short_to_orig", {})

        # Parse the 'output' array produced by Codex
        output = response.get("output")
        if isinstance(output, list):
            for item in output:
                if not isinstance(item, dict):
                    continue
                itype = item.get("type")

                if itype == "reasoning":
                    # Prefer summary (may be array of {type,text}), fallback to content
                    thinking_text = ""
                    summary = item.get("summary")
                    if isinstance(summary, list):
                        parts = []
                        for p in summary:
                            if isinstance(p, dict) and "text" in p:
                                parts.append(str(p.get("text") or ""))
                            else:
                                parts.append(str(p))
                        thinking_text = "".join(parts)
                    elif isinstance(summary, (str, dict)):
                        thinking_text = summary if isinstance(summary, str) else json.dumps(summary, ensure_ascii=False)
                    if not thinking_text:
                        content = item.get("content")
                        if isinstance(content, list):
                            parts = []
                            for p in content:
                                if isinstance(p, dict) and "text" in p:
                                    parts.append(str(p.get("text") or ""))
                                else:
                                    parts.append(str(p))
                            thinking_text = "".join(parts)
                        elif isinstance(content, (str, dict)):
                            thinking_text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
                    if thinking_text:
                        content_blocks.append({"type": "thinking", "thinking": thinking_text, "signature": ""})

                elif itype == "message":
                    cont = item.get("content")
                    if isinstance(cont, list):
                        for p in cont:
                            if isinstance(p, dict) and p.get("type") == "output_text":
                                t = p.get("text", "")
                                if t:
                                    content_blocks.append({"type": "text", "text": t})
                    elif isinstance(cont, str) and cont:
                        content_blocks.append({"type": "text", "text": cont})

                elif itype == "function_call":
                    has_tool_call = True
                    short_name = item.get("name", "function")
                    original_name = short_to_orig.get(short_name, short_name)
                    call_id = item.get("call_id") or item.get("id") or f"call_{uuid.uuid4().hex[:8]}"

                    args_obj = {}
                    args_raw = item.get("arguments")
                    if isinstance(args_raw, str):
                        try:
                            args_obj = json.loads(args_raw)
                        except Exception:
                            args_obj = {"_raw": args_raw}
                    elif isinstance(args_raw, dict):
                        args_obj = args_raw

                    content_blocks.append(
                        {"type": "tool_use", "id": call_id, "name": original_name, "input": args_obj or {}}
                    )

        if content_blocks:
            message["content"] = content_blocks
        else:
            message["content"] = [{"type": "text", "text": ""}]

        # Stop reason: prefer response.stop_reason, else infer
        stop_reason = response.get("stop_reason")
        if stop_reason:
            message["stop_reason"] = stop_reason
        else:
            message["stop_reason"] = "tool_use" if has_tool_call else "end_turn"

        # Stop sequence (if provided)
        stop_seq = response.get("stop_sequence")
        message["stop_sequence"] = stop_seq if stop_seq else None

        return message
