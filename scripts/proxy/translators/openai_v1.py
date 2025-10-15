"""Translator for OpenAI v1 API protocol.

Handles translation between Anthropic Messages API and OpenAI Chat Completions v1 format.
Used for all OpenAI-compatible providers except provider=="openai".
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Dict, List, Optional, Tuple

from ..context import TranslationContext


def anthropic_request_to_openai_v1(
    body: Dict[str, Any],
    context: TranslationContext,
) -> Dict[str, Any]:
    """Convert Anthropic request to OpenAI v1 format."""

    # Build messages array
    messages = []

    # Add system message if present
    system = body.get("system")
    if system:
        if isinstance(system, str):
            messages.append({"role": "system", "content": system})
        elif isinstance(system, list):
            texts = []
            for block in system:
                if isinstance(block, dict) and block.get("type") == "text":
                    texts.append(block.get("text", ""))
            if texts:
                messages.append({"role": "system", "content": "\n".join(texts)})

    # Process conversation messages
    for message in body.get("messages", []):
        role = message.get("role")
        content = message.get("content", [])

        if role == "user":
            user_parts = []
            if isinstance(content, str):
                user_parts.append({"type": "text", "text": content})
            else:
                for block in content:
                    block_type = block.get("type")
                    if block_type == "text":
                        user_parts.append({"type": "text", "text": block.get("text", "")})
                    elif block_type == "image" and block.get("source", {}).get("type") == "base64":
                        source = block["source"]
                        data_url = f"data:{source['media_type']};base64,{source['data']}"
                        user_parts.append({"type": "image_url", "image_url": {"url": data_url}})
                    elif block_type == "tool_result":
                        # Tool result - convert to tool message
                        tool_use_id = block.get("tool_use_id")
                        external_id = context.tools.get_external_id(tool_use_id) if tool_use_id else None
                        result_content = block.get("content", "")
                        if isinstance(result_content, list):
                            # Extract text from content blocks
                            texts = []
                            for item in result_content:
                                if isinstance(item, dict) and item.get("type") == "text":
                                    texts.append(item.get("text", ""))
                            result_content = "\n".join(texts)
                        elif result_content is None:
                            result_content = ""
                        if block.get("is_error"):
                            result_content = json.dumps({
                                "error": True,
                                "content": str(result_content)
                            }, ensure_ascii=False)

                        messages.append({
                            "role": "tool",
                            "tool_call_id": external_id or tool_use_id or f"call_{uuid.uuid4().hex[:8]}",
                            "content": str(result_content),
                        })
                        # Don't add to user_parts, it's a separate message
                        continue

            if user_parts:
                messages.append({"role": "user", "content": user_parts})

        elif role == "assistant":
            text_parts = []
            tool_calls = []

            if isinstance(content, str):
                text_parts.append(content)
            else:
                for block in content:
                    block_type = block.get("type")
                    if block_type == "text":
                        text_parts.append(block.get("text", ""))
                    elif block_type == "tool_use":
                        # Convert to OpenAI tool call
                        anthropic_id = block.get("id")
                        name = block.get("name", "function")
                        input_data = block.get("input", {})

                        # Generate OpenAI-style ID
                        openai_id = f"call_{uuid.uuid4().hex[:16]}"
                        # Register mapping
                        anthropic_tool_id = context.tools.register_tool(openai_id, name)

                        tool_calls.append({
                            "id": openai_id,
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": json.dumps(input_data, ensure_ascii=False),
                            },
                        })

            msg = {"role": "assistant", "content": "".join(text_parts)}
            if tool_calls:
                msg["tool_calls"] = tool_calls
            messages.append(msg)

    # Build OpenAI request
    request = {"messages": messages}

    # Tools
    if body.get("tools"):
        openai_tools = []
        for tool in body["tools"]:
            openai_tools.append({
                "type": "function",
                "function": {
                    "name": tool.get("name", ""),
                    "description": tool.get("description", ""),
                    "parameters": tool.get("input_schema", {"type": "object", "properties": {}}),
                },
            })
        request["tools"] = openai_tools

    # Tool choice
    tool_choice = body.get("tool_choice")
    if tool_choice == "none":
        # Don't send tool_choice for "none" - matches CLIProxyAPI
        pass
    elif tool_choice in ("auto", "any"):
        request["tool_choice"] = "auto"
    elif isinstance(tool_choice, dict) and tool_choice.get("name"):
        request["tool_choice"] = {
            "type": "function",
            "function": {"name": tool_choice["name"]},
        }

    # Stream
    request["stream"] = bool(body.get("stream", False))

    # Other parameters
    if "temperature" in body:
        request["temperature"] = body["temperature"]
    if "top_p" in body:
        request["top_p"] = body["top_p"]
    if "max_tokens" in body:
        request["max_tokens"] = body["max_tokens"]
    if body.get("stop_sequences"):
        stops = body["stop_sequences"]
        request["stop"] = stops if isinstance(stops, list) else [stops]

    # Response format
    response_format = body.get("response_format")
    if isinstance(response_format, dict) and response_format.get("type") == "json_object":
        request["response_format"] = {"type": "json_object"}
    elif response_format == "json":
        request["response_format"] = {"type": "json_object"}

    return request


def openai_v1_response_to_anthropic_streaming(
    chunk: Dict[str, Any],
    context: TranslationContext,
) -> List[Tuple[str, Dict[str, Any]]]:
    """Convert OpenAI v1 streaming chunk to Anthropic SSE events."""

    events = []

    # Initialize on first chunk
    if not context.param:
        context.param = {"message_started": True}
        msg_id = chunk.get("id", f"msg_{uuid.uuid4().hex}")
        events.append(("message_start", {
            "message": {
                "id": msg_id,
                "type": "message",
                "role": "assistant",
                "model": context.requested_model,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            }
        }))

    # Process delta
    choices = chunk.get("choices", [])
    if choices:
        choice = choices[0]
        delta = choice.get("delta", {})

        # Text content
        text = delta.get("content")
        if text:
            if not context.streaming.text_started:
                idx = context.streaming.get_text_index()
                events.append(("content_block_start", {
                    "index": idx,
                    "type": "text",
                }))
                context.streaming.text_started = True

            events.append(("content_block_delta", {
                "index": context.streaming.text_index,
                "delta": {"type": "text_delta", "text": text},
            }))

        # Tool calls
        tool_deltas = delta.get("tool_calls")
        if isinstance(tool_deltas, list):
            for tool_delta in tool_deltas:
                openai_index = tool_delta.get("index", 0)

                # Initialize tool state if needed
                if openai_index not in context.streaming.tool_states:
                    # Allocate Anthropic index
                    anth_index = context.streaming.allocate_index()
                    context.streaming.tool_states[openai_index] = {
                        "anth_index": anth_index,
                        "openai_id": None,
                        "anth_id": None,
                        "name": None,
                        "arguments": "",
                        "started": False,
                        "stopped": False,
                    }

                state = context.streaming.tool_states[openai_index]

                # Update tool info
                if tool_delta.get("id"):
                    state["openai_id"] = tool_delta["id"]
                    state["anth_id"] = context.tools.get_anthropic_id(tool_delta["id"])

                function = tool_delta.get("function", {})
                if function.get("name"):
                    state["name"] = function["name"]
                    # Register tool
                    if state["openai_id"]:
                        context.tools.register_tool(state["openai_id"], function["name"])

                # Start block if needed
                if not state["started"] and state["anth_id"] and state["name"]:
                    events.append(("content_block_start", {
                        "index": state["anth_index"],
                        "type": "tool_use",
                        "id": state["anth_id"],
                        "name": state["name"],
                        "input": {},
                    }))
                    # Send initial empty delta
                    events.append(("content_block_delta", {
                        "index": state["anth_index"],
                        "delta": {"type": "input_json_delta", "partial_json": ""},
                    }))
                    state["started"] = True

                # Send arguments delta
                if function.get("arguments") and state["started"]:
                    args = function["arguments"]
                    # Detect incremental vs full arguments
                    prev_args = state["arguments"]
                    if args.startswith(prev_args):
                        # Incremental - send only new part
                        delta_args = args[len(prev_args):]
                    else:
                        # Full replacement
                        delta_args = args

                    state["arguments"] = args

                    if delta_args:
                        events.append(("content_block_delta", {
                            "index": state["anth_index"],
                            "delta": {"type": "input_json_delta", "partial_json": delta_args},
                        }))

        # Handle finish reason
        finish_reason = choice.get("finish_reason")
        if finish_reason:
            # Close open blocks
            if context.streaming.text_started:
                events.append(("content_block_stop", {"index": context.streaming.text_index}))
                context.streaming.text_started = False

            for state in context.streaming.tool_states.values():
                if state["started"] and not state["stopped"]:
                    events.append(("content_block_stop", {"index": state["anth_index"]}))
                    state["stopped"] = True

            # Map finish reason
            if finish_reason == "tool_calls":
                stop_reason = "tool_use"
            elif finish_reason == "length":
                stop_reason = "max_tokens"
            elif finish_reason == "stop":
                stop_reason = "end_turn"
            else:
                stop_reason = "end_turn"

            context.streaming.finish_reason = stop_reason

    # Usage information
    usage = chunk.get("usage")
    if usage:
        context.streaming.input_tokens = usage.get("prompt_tokens")
        context.streaming.output_tokens = usage.get("completion_tokens")

        if context.streaming.finish_reason:
            events.append(("message_delta", {
                "delta": {"stop_reason": context.streaming.finish_reason},
                "usage": {
                    "input_tokens": context.streaming.input_tokens or 0,
                    "output_tokens": context.streaming.output_tokens or 0,
                },
            }))
            events.append(("message_stop", {"type": "message_stop"}))

    return events


def openai_v1_response_to_anthropic(
    response: Dict[str, Any],
    context: TranslationContext,
) -> Dict[str, Any]:
    """Convert non-streaming OpenAI v1 response to Anthropic format."""

    choice = response.get("choices", [{}])[0]
    message = choice.get("message", {})

    # Build content blocks
    content = []

    # Text content
    text = message.get("content", "")
    if text:
        content.append({"type": "text", "text": text})

    # Tool calls
    for tool_call in message.get("tool_calls", []):
        openai_id = tool_call.get("id")
        function = tool_call.get("function", {})
        name = function.get("name", "function")

        # Get Anthropic ID
        anthropic_id = context.tools.get_anthropic_id(openai_id) if openai_id else context.tools.register_tool(
            openai_id or f"call_{uuid.uuid4().hex[:8]}",
            name
        )

        # Parse arguments
        args = {}
        if function.get("arguments"):
            try:
                args = json.loads(function["arguments"])
            except Exception:
                args = {"_raw": function["arguments"]}

        content.append({
            "type": "tool_use",
            "id": anthropic_id,
            "name": name,
            "input": args,
        })

    # Map finish reason
    finish_reason = choice.get("finish_reason", "stop")
    if finish_reason == "tool_calls":
        stop_reason = "tool_use"
    elif finish_reason == "length":
        stop_reason = "max_tokens"
    else:
        stop_reason = "end_turn"

    # Usage
    usage_data = response.get("usage", {})
    usage = {
        "input_tokens": usage_data.get("prompt_tokens", 0),
        "output_tokens": usage_data.get("completion_tokens", 0),
    }

    return {
        "id": f"msg_{uuid.uuid4().hex}",
        "type": "message",
        "role": "assistant",
        "model": context.requested_model,
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": usage,
    }