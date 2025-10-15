"""Anthropic native provider executor."""

from __future__ import annotations

import asyncio
from typing import Any, Dict

from aiohttp import web
from aiohttp.client_exceptions import ClientConnectionError

from .. import logging_control
from ..auth import resolve_auth_strategy
from ..errors import anthropic_error_payload, extract_error_details
import json
from .base import ProviderExecutor


ANTHROPIC_VERSION = "2023-06-01"


async def _read_json(response) -> Dict[str, Any]:
    try:
        return await response.json()
    except Exception:
        try:
            text = await response.text()
            return {"message": text}
        except Exception:
            return {"message": "unknown upstream error"}


class AnthropicExecutor(ProviderExecutor):
    # Token budget mapping for reasoning levels
    REASONING_TOKEN_MAP = {
        "low": 2048,
        "medium": 8192,
        "high": 32768,
    }

    async def execute(self, request: web.Request) -> web.StreamResponse:
        body = dict(self.request_body)
        body.setdefault("metadata", dict(self.metadata))
        body["model"] = self.cfg.model

        base = self.cfg.base_url.rstrip('/') if self.cfg.base_url else 'https://api.anthropic.com'
        url = f"{base}/v1/messages"
        auth_method = (self.cfg.auth_method or 'api_key').lower()
        auth = resolve_auth_strategy(self.cfg.provider, self.cfg)
        headers: Dict[str, str] = {
            "Content-Type": "application/json",
            "anthropic-version": ANTHROPIC_VERSION,
        }

        if auth_method == 'oauth':
            url = f"{base}/v1/messages?beta=true"
            headers.update({
                "Anthropic-Beta": "claude-code-20250219,oauth-2025-04-20,interleaved-thinking-2025-05-14,fine-grained-tool-streaming-2025-05-14",
                "User-Agent": "claude-cli/1.0.83 (external, cli)",
                "X-App": "cli",
                "X-Stainless-Helper-Method": "stream",
                "X-Stainless-Lang": "js",
                "X-Stainless-Runtime": "node",
                "X-Stainless-Runtime-Version": "v24.3.0",
                "X-Stainless-Package-Version": "0.55.1",
                "Anthropic-Dangerous-Direct-Browser-Access": "true",
            })
        headers.update(auth.headers())

        # Extract reasoning level from extra_headers (if present)
        extra_headers = dict(self.cfg.extra_headers or {})
        reasoning_level = extra_headers.pop("reasoning", None)

        # Add thinking object to request body if reasoning level specified
        if reasoning_level and reasoning_level.lower() in self.REASONING_TOKEN_MAP:
            body["thinking"] = {
                "type": "enabled",
                "budget_tokens": self.REASONING_TOKEN_MAP[reasoning_level.lower()]
            }

        # Add remaining extra_headers to HTTP headers
        headers.update(extra_headers)

        self._log_upstream(url, headers, body)

        stream = bool(self.request_body.get("stream", False))

        try:
            async with self._client_session() as session:
                async with session.post(url, json=body, headers=headers) as upstream:
                    if logging_control.is_enabled():
                        print("\nUPSTREAM RESPONSE:")
                        print(f"   Status: {upstream.status}")
                        print(f"   Headers: {dict(upstream.headers)}")

                    if upstream.status >= 400:
                        error_body = await _read_json(upstream)
                        error_type, message = extract_error_details(error_body)

                        if stream:
                            resp = web.StreamResponse(status=upstream.status, headers={"Content-Type": "text/event-stream"})
                            await resp.prepare(request)
                            # Format SSE events directly
                            error_event = f"event: error\ndata: {json.dumps(anthropic_error_payload(message, error_type))}\n\n"
                            await resp.write(error_event.encode('utf-8'))
                            stop_event = f"event: message_stop\ndata: {json.dumps({'type': 'message_stop', 'stop_reason': 'error'})}\n\n"
                            await resp.write(stop_event.encode('utf-8'))
                            await resp.write_eof()
                            return resp

                        return web.json_response(
                            anthropic_error_payload(message, error_type),
                            status=upstream.status,
                        )

                    if stream:
                        resp = web.StreamResponse(status=200, headers={"Content-Type": "text/event-stream"})
                        await resp.prepare(request)

                        # Process line by line exactly like CLIProxyAPI
                        buffer = b""
                        try:
                            async for chunk in upstream.content.iter_any():
                                if not chunk:
                                    continue
                                buffer += chunk
                                # Process lines as they come
                                while b"\n" in buffer:
                                    line, buffer = buffer.split(b"\n", 1)
                                    # Write line with proper SSE formatting
                                    if line.startswith(b"event:"):
                                        await resp.write(b"\n")  # Newline before event
                                    await resp.write(line)
                                    await resp.write(b"\n")  # Newline after

                            # Handle any remaining data
                            if buffer:
                                if buffer.startswith(b"event:"):
                                    await resp.write(b"\n")
                                await resp.write(buffer)
                                await resp.write(b"\n")

                        except (ConnectionResetError, ClientConnectionError) as exc:
                            print(f"Client disconnected during Anthropic streaming: {exc}")

                        return resp

                    payload = await upstream.json()
                    return web.json_response(payload)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if logging_control.is_enabled():
                print(f"Anthropic executor error: {exc}")
            return web.json_response(
                anthropic_error_payload(f"Upstream error: {exc}"),
                status=502,
            )


__all__ = ["AnthropicExecutor"]
