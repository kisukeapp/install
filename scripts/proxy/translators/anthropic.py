"""Utilities for converting Anthropic payloads to OpenAI-compatible formats."""

from __future__ import annotations

import json
import uuid
from typing import Any, Dict, List, Optional, Tuple


def sanitize_json_schema(schema: Any) -> Any:
    """Drop validation fields that commonly break with upstream providers."""
    if isinstance(schema, dict):
        cleaned = {}
        for key, value in schema.items():
            if key == "format":
                continue
            cleaned[key] = sanitize_json_schema(value)
        return cleaned
    if isinstance(schema, list):
        return [sanitize_json_schema(item) for item in schema]
    return schema


def anthropic_tools_to_openai(tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Convert Anthropic tool definitions into OpenAI function-call schema."""
    out: List[Dict[str, Any]] = []
    for tool in tools or []:
        name = tool.get("name")
        if not name:
            continue
        desc = tool.get("description", "")
        params = tool.get("input_schema", {"type": "object", "properties": {}})
        out.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": desc,
                    "parameters": sanitize_json_schema(params),
                },
            }
        )
    return out


def anthropic_tools_to_codex(tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Convert Anthropic tools into the flatter Codex response format."""
    out: List[Dict[str, Any]] = []
    for tool in tools or []:
        name = tool.get("name")
        if not name:
            continue
        desc = tool.get("description", "")
        params = tool.get("input_schema", {"type": "object", "properties": {}})
        out.append(
            {
                "name": name,
                "description": desc,
                "parameters": sanitize_json_schema(params),
            }
        )
    return out


def anthropic_tool_choice_to_openai(choice: Any) -> Any:
    """Map Anthropic tool_choice structures to OpenAI equivalents."""
    if choice in (None, "auto", "any"):
        return "auto"
    if choice == "none":
        return None
    if isinstance(choice, dict):
        name = choice.get("name")
        if name:
            return {"type": "function", "function": {"name": name}}
    return "auto"


def is_base64_src(block: Dict[str, Any]) -> bool:
    """Return True when a content block contains an inline base64 source."""
    source = block.get("source", {})
    return (
        source.get("type") == "base64"
        and bool(source.get("media_type"))
        and bool(source.get("data"))
    )


def flatten_system_to_text(system: Any) -> str:
    """Flatten Anthropic system content to a simple string for logging/debugging."""
    if system is None:
        return ""
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        return "\n".join(
            part.get("text", "")
            for part in system
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return str(system)


def anthropic_system_to_openai(system: Any) -> Optional[Dict[str, Any]]:
    """Convert Anthropic system content to a chat.completions system message."""
    if system is None:
        return None
    if isinstance(system, str):
        return {"role": "system", "content": system}
    if isinstance(system, list):
        texts: List[str] = []
        for part in system:
            if isinstance(part, dict) and part.get("type") == "text":
                texts.append(part.get("text", ""))
        if texts:
            return {"role": "system", "content": "\n".join(texts)}
    return None


def anthropic_messages_to_openai(
    messages: List[Dict[str, Any]]
) -> Tuple[List[Dict[str, Any]], Dict[str, str]]:
    """Convert Anthropic conversation blocks into OpenAI chat messages."""
    out: List[Dict[str, Any]] = []
    tool_id_name: Dict[str, str] = {}

    for message in messages or []:
        role = message.get("role")
        content = message.get("content", [])

        if role == "user":
            user_parts: List[Dict[str, Any]] = []
            if isinstance(content, str):
                user_parts.append({"type": "text", "text": content})
            else:
                for block in content:
                    block_type = block.get("type")
                    if block_type == "text":
                        user_parts.append({"type": "text", "text": block.get("text", "")})
                    elif block_type == "image" and is_base64_src(block):
                        source = block["source"]
                        data_url = f"data:{source['media_type']};base64,{source['data']}"
                        user_parts.append({"type": "image_url", "image_url": {"url": data_url}})
                    elif block_type == "tool_result":
                        tool_use_id = (
                            block.get("tool_use_id")
                            or block.get("id")
                            or f"tool_{uuid.uuid4().hex[:8]}"
                        )
                        result_content = block.get("content")
                        if isinstance(result_content, list):
                            texts = []
                            for item in result_content:
                                if isinstance(item, dict) and item.get("type") == "text":
                                    texts.append(item.get("text", ""))
                            result_content = "\n".join(texts)
                        if result_content is None:
                            result_content = ""
                        if block.get("is_error"):
                            result_content = json.dumps(
                                {"error": True, "content": result_content},
                                ensure_ascii=False,
                            )
                        out.append(
                            {
                                "role": "tool",
                                "tool_call_id": tool_use_id,
                                "content": str(result_content),
                            }
                        )
            if user_parts:
                out.append({"role": "user", "content": user_parts})

        elif role == "assistant":
            text_acc: List[str] = []
            tool_calls: List[Dict[str, Any]] = []
            if isinstance(content, str):
                text_acc.append(content)
            else:
                for block in content:
                    block_type = block.get("type")
                    if block_type == "text":
                        text_acc.append(block.get("text", ""))
                    elif block_type == "tool_use":
                        tool_name = block.get("name") or "function"
                        tool_id = block.get("id") or f"tool_{uuid.uuid4().hex[:8]}"
                        tool_input = block.get("input", {})
                        tool_id_name[tool_id] = tool_name
                        tool_calls.append(
                            {
                                "id": tool_id,
                                "type": "function",
                                "function": {
                                    "name": tool_name,
                                    "arguments": json.dumps(tool_input, ensure_ascii=False),
                                },
                            }
                        )
            message_payload: Dict[str, Any] = {"role": "assistant", "content": "".join(text_acc)}
            if tool_calls:
                message_payload["tool_calls"] = tool_calls
            out.append(message_payload)

        elif role == "tool":
            out.append(
                {
                    "role": "tool",
                    "tool_call_id": message.get("tool_call_id", f"tool_{uuid.uuid4().hex[:8]}"),
                    "content": str(message.get("content", "")),
                }
            )

        elif role == "system":
            out.append({"role": "system", "content": str(message.get("content", ""))})

    return out, tool_id_name


def map_anthropic_request_to_openai(
    body: Dict[str, Any]
) -> Tuple[Dict[str, Any], Dict[str, str]]:
    """Build an OpenAI chat.completions payload from an Anthropic request body."""
    system_message = anthropic_system_to_openai(body.get("system"))
    messages, tool_id_map = anthropic_messages_to_openai(body.get("messages", []))
    if system_message:
        messages.insert(0, system_message)

    request: Dict[str, Any] = {"messages": messages}

    request["stream"] = bool(body.get("stream", False))

    if "temperature" in body:
        request["temperature"] = body["temperature"]
    if body.get("top_p") is not None:
        request["top_p"] = body["top_p"]

    if body.get("stop_sequences"):
        stops = body["stop_sequences"]
        request["stop"] = stops if isinstance(stops, list) else [stops]

    if isinstance(body.get("max_tokens"), int):
        request["max_tokens"] = body["max_tokens"]

    if body.get("tools"):
        request["tools"] = anthropic_tools_to_openai(body["tools"])
    if "tool_choice" in body:
        tool_choice = anthropic_tool_choice_to_openai(body["tool_choice"])
        if tool_choice is not None:
            request["tool_choice"] = tool_choice

    response_format = body.get("response_format")
    if isinstance(response_format, dict) and response_format.get("type") == "json_object":
        request["response_format"] = {"type": "json_object"}
    elif response_format == "json":
        request["response_format"] = {"type": "json_object"}

    return request, tool_id_map


__all__ = [
    "anthropic_messages_to_openai",
    "anthropic_system_to_openai",
    "anthropic_tool_choice_to_openai",
    "anthropic_tools_to_codex",
    "anthropic_tools_to_openai",
    "flatten_system_to_text",
    "is_base64_src",
    "map_anthropic_request_to_openai",
    "sanitize_json_schema",
]
