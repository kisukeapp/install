"""Helpers for emitting Anthropic-compatible streaming events.

These utilities encapsulate shared logic for non-Anthropic providers that need to
translate upstream streaming responses into the Claude Messages SSE protocol.
"""

from __future__ import annotations

from typing import Dict, Optional

from aiohttp import web

from ..sse import sse_event


def map_stop_reason(raw_reason: Optional[str], *, tool_used: bool = False) -> str:
    """Normalize provider-specific finish reasons to Anthropic stop reasons.

    Claude expects stop reasons like ``end_turn`` or ``tool_use``. Upstream providers
    use a variety of labels (``stop``, ``tool_calls``, ``length``). We map the most
    common variants here and fall back to ``end_turn`` when nothing specific is
    provided.
    """

    if tool_used:
        return "tool_use"
    if not raw_reason:
        return "end_turn"

    normalized = raw_reason.lower()
    if normalized == "tool_calls":
        return "tool_use"
    if normalized in {"stop", "stop_sequence"}:
        return "end_turn"
    if normalized == "length":
        return "max_tokens"
    if normalized == "content_filter":
        return "content_filter"
    return normalized


async def emit_message_tail(
    resp: web.StreamResponse,
    *,
    stop_reason: str,
    usage: Optional[Dict[str, Optional[int]]] = None,
) -> None:
    """Send the trailing ``message_delta`` and ``message_stop`` SSE events.

    The Anthropic CLI expects a ``message_delta`` announcing the final stop reason
    (and token usage when available) before the terminal ``message_stop`` event.
    Centralising this behaviour keeps provider executors consistent and makes it
    easier to add usage reporting across multiple backends.
    """

    message_delta = {
        "type": "message_delta",
        "delta": {
            "stop_reason": stop_reason,
            "stop_sequence": None,
        },
    }
    if usage and any(value is not None for value in usage.values()):
        message_delta["usage"] = usage

    await resp.write(sse_event("message_delta", message_delta))
    await resp.write(
        sse_event(
            "message_stop",
            {
                "type": "message_stop",
                "stop_reason": stop_reason,
            },
        )
    )


__all__ = ["emit_message_tail", "map_stop_reason"]
