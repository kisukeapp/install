"""Shared helpers for Server-Sent Events handling."""

from __future__ import annotations

import json
import uuid
from typing import Any, AsyncIterator, Dict, Tuple

from aiohttp import ClientResponse


def sse_event(event_type: str, data_obj: Dict[str, Any]) -> bytes:
    return f"event: {event_type}\ndata: {json.dumps(data_obj, ensure_ascii=False)}\n\n".encode("utf-8")


def new_message_stub(model_id: str) -> Dict[str, Any]:
    """Return a baseline Claude message payload for ``message_start`` events."""

    return {
        "type": "message",
        "id": f"msg_{uuid.uuid4().hex}",
        "role": "assistant",
        "model": model_id,
        "stop_reason": None,
        "stop_sequence": None,
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }


async def iter_openai_sse(resp: ClientResponse) -> AsyncIterator[Dict[str, Any]]:
    buffer = b""
    async for chunk in resp.content.iter_any():
        if not chunk:
            continue
        buffer += chunk
        *lines, buffer = buffer.split(b"\n")
        for raw in lines:
            line = raw.strip()
            if not line.startswith(b"data:"):
                continue
            data = line[5:].strip()
            if data == b"[DONE]":
                return
            try:
                yield json.loads(data.decode("utf-8", errors="ignore"))
            except Exception:
                continue


async def iter_codex_sse(resp: ClientResponse) -> AsyncIterator[Tuple[str, Dict[str, Any]]]:
    buffer = b""
    async for chunk in resp.content.iter_any():
        if not chunk:
            continue
        buffer += chunk
        while b"\n\n" in buffer:
            block, buffer = buffer.split(b"\n\n", 1)
            if not block:
                continue
            event_name = None
            event_data = None
            for line in block.split(b"\n"):
                line = line.strip()
                if line.startswith(b"event:"):
                    event_name = line[6:].strip().decode("utf-8", errors="ignore")
                elif line.startswith(b"data:"):
                    data_str = line[5:].strip()
                    if data_str and data_str != b"[DONE]":
                        try:
                            event_data = json.loads(data_str.decode("utf-8", errors="ignore"))
                        except Exception:
                            event_data = None
            if event_name and event_data is not None:
                yield event_name, event_data


async def iter_anthropic_sse(resp: ClientResponse) -> AsyncIterator[bytes]:
    """Stream SSE data line by line, exactly like CLIProxyAPI."""
    buffer = b""
    async for chunk in resp.content.iter_any():
        if not chunk:
            continue
        buffer += chunk
        # Process line by line, not event by event
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            # Send each line with its newline
            yield line + b"\n"
    # Send any remaining data
    if buffer:
        yield buffer


__all__ = [
    "sse_event",
    "new_message_stub",
    "iter_openai_sse",
    "iter_codex_sse",
    "iter_anthropic_sse",
]
