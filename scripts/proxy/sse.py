"""Shared helpers for Server-Sent Events handling."""

from __future__ import annotations

import json
import uuid
from typing import Any, AsyncIterator, Dict, Tuple, List

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
    # Flush any remaining buffered data (in case upstream ended without newline)
    if buffer:
        line = buffer.strip()
        if line.startswith(b"data:"):
            data = line[5:].strip()
            if data and data != b"[DONE]":
                try:
                    yield json.loads(data.decode("utf-8", errors="ignore"))
                except Exception:
                    pass


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
    """Stream SSE data line by line."""
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


def frames_from_final_message(message: Dict[str, Any]) -> List[bytes]:
    """Synthesize Anthropic-compatible SSE frames from a final message JSON.

    This is used as a fallback when an upstream provider returns a non-streaming
    JSON response but the client expects SSE. We emit a minimal, coherent
    sequence of events:
      - message_start
      - content_block_start/delta/stop for each content block
      - message_delta with stop_reason and usage
      - message_stop
    """
    frames: List[bytes] = []

    # Build message_start with an empty content array; usage is filled by message_delta
    msg_id = message.get("id") or f"msg_{uuid.uuid4().hex}"
    model_id = message.get("model") or ""
    start_payload: Dict[str, Any] = {
        "type": "message_start",
        "message": {
            "id": msg_id,
            "type": "message",
            "role": "assistant",
            "model": model_id,
            "content": [],
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        },
    }
    frames.append(sse_event("message_start", start_payload))

    # Emit content blocks
    index = 0
    for block in message.get("content", []) or []:
        btype = block.get("type")
        if btype == "text":
            text = block.get("text") or ""
            frames.append(
                sse_event(
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": index,
                        "content_block": {"type": "text", "text": ""},
                    },
                )
            )
            if text:
                frames.append(
                    sse_event(
                        "content_block_delta",
                        {
                            "type": "content_block_delta",
                            "index": index,
                            "delta": {"type": "text_delta", "text": text},
                        },
                    )
                )
            frames.append(sse_event("content_block_stop", {"type": "content_block_stop", "index": index}))
            index += 1

        elif btype == "thinking":
            thinking = block.get("thinking") or ""
            frames.append(
                sse_event(
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": index,
                        "content_block": {"type": "thinking", "thinking": "", "signature": ""},
                    },
                )
            )
            if thinking:
                frames.append(
                    sse_event(
                        "content_block_delta",
                        {
                            "type": "content_block_delta",
                            "index": index,
                            "delta": {"type": "thinking_delta", "thinking": thinking},
                        },
                    )
                )
            frames.append(sse_event("content_block_stop", {"type": "content_block_stop", "index": index}))
            index += 1

        elif btype == "tool_use":
            call_id = block.get("id") or f"call_{uuid.uuid4().hex[:8]}"
            name = block.get("name") or "function"
            inp = block.get("input") or {}
            frames.append(
                sse_event(
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": index,
                        "content_block": {"type": "tool_use", "id": call_id, "name": name, "input": {}},
                    },
                )
            )
            frames.append(
                sse_event(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": index,
                        "delta": {"type": "input_json_delta", "partial_json": json.dumps(inp, ensure_ascii=False)},
                    },
                )
            )
            frames.append(sse_event("content_block_stop", {"type": "content_block_stop", "index": index}))
            index += 1

        # ignore other content types for SSE fallback

    # message_delta with stop_reason and usage
    stop_reason = message.get("stop_reason") or "end_turn"
    usage = message.get("usage") or {}
    out_tokens = usage.get("output_tokens", 0) or 0
    in_tokens = usage.get("input_tokens", 0) or 0
    delta_payload: Dict[str, Any] = {
        "type": "message_delta",
        "delta": {"stop_reason": stop_reason},
        "usage": {"input_tokens": in_tokens, "output_tokens": out_tokens},
    }
    # Optional fields that some translators populate
    for opt_key in ("thinking_tokens", "cache_read_input_tokens"):
        if opt_key in usage:
            delta_payload["usage"][opt_key] = usage[opt_key]
    frames.append(sse_event("message_delta", delta_payload))

    frames.append(sse_event("message_stop", {"type": "message_stop"}))
    return frames
