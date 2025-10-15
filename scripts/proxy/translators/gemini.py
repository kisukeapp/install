"""Translator for Gemini native API format.

This module handles conversion between Anthropic (Claude) message format
and Google's Gemini API format.
"""

from __future__ import annotations

import copy
import json
import uuid
import hashlib
from typing import Any, Dict, List, Optional


def _sanitize_schema_for_gemini(schema: Dict[str, Any]) -> Dict[str, Any]:
    """Remove JSON Schema fields that are incompatible with Gemini API.

    Gemini API rejects certain JSON Schema fields that are part of the standard
    but not supported in function declarations. This function recursively removes:
    - additionalProperties: Not supported in Gemini function declarations
    - $schema: JSON Schema meta-schema identifier, not needed for API
    - allOf/anyOf/oneOf: Union type constructs not supported
    - exclusiveMinimum/exclusiveMaximum: Advanced validation constraints
    - patternProperties: Advanced property pattern matching
    - dependencies: Property dependencies not supported

    Also converts type arrays (e.g., ["string", "null"]) to single types.

    Args:
        schema: The JSON schema dict to sanitize

    Returns:
        A sanitized copy of the schema
    """
    # Make a deep copy to avoid mutating the original
    result = copy.deepcopy(schema)

    # Fields to remove at any level
    fields_to_remove = [
        "additionalProperties",
        "$schema",
        "allOf",
        "anyOf",
        "oneOf",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "patternProperties",
        "dependencies",
    ]

    def clean_dict(obj: Any) -> Any:
        """Recursively clean a dictionary."""
        if not isinstance(obj, dict):
            if isinstance(obj, list):
                return [clean_dict(item) for item in obj]
            return obj

        # Remove incompatible fields
        for field in fields_to_remove:
            obj.pop(field, None)

        # Handle type arrays - convert to single type
        if "type" in obj and isinstance(obj["type"], list):
            type_array = obj["type"]
            # Prioritize string, then number/integer, then others
            preferred_type = None
            for t in type_array:
                if t == "string":
                    preferred_type = "string"
                    break
                elif t in ("number", "integer") and preferred_type is None:
                    preferred_type = t
                elif preferred_type is None:
                    preferred_type = t
            if preferred_type:
                obj["type"] = preferred_type

        # Recursively clean nested objects
        for key, value in obj.items():
            obj[key] = clean_dict(value)

        return obj

    return clean_dict(result)


def anthropic_request_to_gemini(
    request_body: Dict[str, Any],
    model: str,
    system_instruction: Optional[str] = None,
    reasoning_level: Optional[str] = None,
) -> Dict[str, Any]:
    """Convert Anthropic request format to Gemini format.

    Handles:
    - Messages array → Contents array with role mapping
    - System messages → system_instruction field
    - Tool use → functionCall format
    - Tool results → functionResponse format
    - Reasoning level → thinkingBudget mapping
    """

    # Token budget mapping for reasoning levels
    REASONING_TOKEN_MAP = {
        "low": 1024,
        "medium": 4096,
        "high": 16384,
    }

    gemini_body = {
        "contents": [],
        "generationConfig": {
            # Default thinkingConfig per CLIProxyAPI spec
            "thinkingConfig": {
                "include_thoughts": True,
                "thinkingBudget": -1  # Auto mode by default
            }
        }
    }

    # Override thinkingBudget if reasoning level specified
    if reasoning_level and reasoning_level.lower() in REASONING_TOKEN_MAP:
        gemini_body["generationConfig"]["thinkingConfig"]["thinkingBudget"] = REASONING_TOKEN_MAP[reasoning_level.lower()]

    # Track tool_use_id -> function name mapping for tool results
    # Anthropic tool_result references tool_use_id, but Gemini functionResponse needs function name
    tool_id_to_name: Dict[str, str] = {}

    # Extract generation config from Anthropic format
    if "max_tokens" in request_body:
        gemini_body["generationConfig"]["maxOutputTokens"] = request_body["max_tokens"]

    if "temperature" in request_body:
        gemini_body["generationConfig"]["temperature"] = request_body["temperature"]

    if "top_p" in request_body:
        gemini_body["generationConfig"]["topP"] = request_body["top_p"]

    if "stop_sequences" in request_body and request_body["stop_sequences"]:
        gemini_body["generationConfig"]["stopSequences"] = request_body["stop_sequences"]

    # Handle thinking/reasoning configuration
    # Override defaults if explicit thinking config provided
    if "thinking" in request_body and isinstance(request_body["thinking"], dict):
        thinking = request_body["thinking"]
        if thinking.get("type") == "enabled":
            gemini_body["generationConfig"]["thinkingConfig"]["include_thoughts"] = True
            if "budget_tokens" in thinking:
                gemini_body["generationConfig"]["thinkingConfig"]["thinkingBudget"] = thinking["budget_tokens"]
        elif thinking.get("type") == "disabled":
            gemini_body["generationConfig"]["thinkingConfig"]["include_thoughts"] = False
            gemini_body["generationConfig"]["thinkingConfig"]["thinkingBudget"] = 0

    # Process system messages and instructions
    system_parts = []

    # Add explicit system instruction if provided (from function parameter)
    if system_instruction:
        system_parts.append({"text": system_instruction})

    # Extract system from TOP-LEVEL "system" field (Anthropic format)
    # This is where the main system prompt lives, NOT in messages array
    if "system" in request_body:
        system_content = request_body["system"]
        if isinstance(system_content, str):
            # Simple string system prompt
            system_parts.append({"text": system_content})
        elif isinstance(system_content, list):
            # Array of system content blocks
            for item in system_content:
                if isinstance(item, dict) and item.get("type") == "text":
                    system_parts.append({"text": item.get("text", "")})
                elif isinstance(item, str):
                    system_parts.append({"text": item})

    # Extract system messages from the messages array (legacy/alternative format)
    messages = request_body.get("messages", [])
    regular_messages = []

    for msg in messages:
        if msg.get("role") == "system":
            # Extract text from system message content
            content = msg.get("content", "")
            if isinstance(content, str):
                system_parts.append({"text": content})
            elif isinstance(content, list):
                for item in content:
                    if item.get("type") == "text":
                        system_parts.append({"text": item.get("text", "")})
        else:
            regular_messages.append(msg)

    # Add system instruction to Gemini format if we have any
    # Per CLIProxyAPI spec, systemInstruction must be a Content object with role="user"
    if system_parts:
        gemini_body["system_instruction"] = {
            "role": "user",
            "parts": system_parts
        }

    # Convert regular messages to contents
    for msg in regular_messages:
        role = msg.get("role", "user")

        # Map Anthropic roles to Gemini roles
        if role == "assistant":
            role = "model"

        gemini_content = {
            "role": role,
            "parts": []
        }

        content = msg.get("content", [])
        if isinstance(content, str):
            # Simple text content
            gemini_content["parts"].append({"text": content})
        elif isinstance(content, list):
            # Process content array
            for item in content:
                item_type = item.get("type")

                if item_type == "text":
                    gemini_content["parts"].append({"text": item.get("text", "")})

                elif item_type == "tool_use":
                    # Convert Anthropic tool_use to Gemini functionCall
                    tool_name = item.get("name", "")
                    tool_id = item.get("id", "")

                    # Store mapping for later tool_result processing
                    if tool_id and tool_name:
                        tool_id_to_name[tool_id] = tool_name

                    function_call = {
                        "functionCall": {
                            "name": tool_name,
                            "args": item.get("input", {})
                        }
                    }
                    gemini_content["parts"].append(function_call)

                elif item_type == "tool_result":
                    # Convert Anthropic tool_result to Gemini functionResponse
                    tool_use_id = item.get("tool_use_id", "")

                    # Look up the function name from our mapping
                    # Gemini functionResponse needs the function name, not the tool_use_id
                    function_name = tool_id_to_name.get(tool_use_id, tool_use_id)

                    function_response = {
                        "functionResponse": {
                            "name": function_name,
                            "response": {}
                        }
                    }

                    # Extract content from tool result
                    tool_content = item.get("content", "")
                    if isinstance(tool_content, str):
                        function_response["functionResponse"]["response"] = {"result": tool_content}
                    elif isinstance(tool_content, list):
                        # Handle complex tool results
                        result_text = []
                        for result_item in tool_content:
                            if result_item.get("type") == "text":
                                result_text.append(result_item.get("text", ""))
                        if result_text:
                            function_response["functionResponse"]["response"] = {"result": "\n".join(result_text)}

                    gemini_content["parts"].append(function_response)

                elif item_type == "image":
                    # Handle image content
                    if "data" in item and "media_type" in item:
                        inline_data = {
                            "inlineData": {
                                "mimeType": item["media_type"],
                                "data": item["data"]
                            }
                        }
                        gemini_content["parts"].append(inline_data)

        if gemini_content["parts"]:
            gemini_body["contents"].append(gemini_content)

    # Handle tools/functions declaration
    # IMPORTANT: ALL tools must be in ONE ToolDeclaration with multiple functionDeclarations
    # Per CLIProxyAPI spec: tools = [{functionDeclarations: [tool1, tool2, ...]}]
    if "tools" in request_body and request_body["tools"]:
        function_declarations = []
        for tool in request_body["tools"]:
            # Anthropic format has tools as array of {name, description, input_schema}
            # Gemini format needs functionDeclarations with {name, description, parameters}
            tool_name = tool.get("name", "")
            tool_desc = tool.get("description", "")
            tool_schema = tool.get("input_schema", {})

            # If tool has nested "function" object (some formats), extract from there
            if "function" in tool:
                func_def = tool["function"]
                tool_name = func_def.get("name", tool_name)
                tool_desc = func_def.get("description", tool_desc)
                tool_schema = func_def.get("parameters", tool_schema)

            # CRITICAL: Sanitize schema for Gemini compatibility
            # Gemini API rejects $schema and additionalProperties fields
            tool_schema = _sanitize_schema_for_gemini(tool_schema)

            function_declarations.append({
                "name": tool_name,
                "description": tool_desc,
                "parameters": tool_schema
            })

        if function_declarations:
            # Create single ToolDeclaration containing all functions
            gemini_body["tools"] = [{
                "functionDeclarations": function_declarations
            }]

    # Handle tool choice
    if "tool_choice" in request_body:
        tool_choice = request_body["tool_choice"]
        if isinstance(tool_choice, dict):
            choice_type = tool_choice.get("type")
            if choice_type == "any":
                gemini_body["toolConfig"] = {
                    "functionCallingConfig": {
                        "mode": "ANY"
                    }
                }
            elif choice_type == "tool" and "name" in tool_choice:
                gemini_body["toolConfig"] = {
                    "functionCallingConfig": {
                        "mode": "ANY",
                        "allowedFunctionNames": [tool_choice["name"]]
                    }
                }
            elif choice_type == "none":
                gemini_body["toolConfig"] = {
                    "functionCallingConfig": {
                        "mode": "NONE"
                    }
                }

    return gemini_body


def gemini_response_to_anthropic(
    gemini_response: Dict[str, Any],
    request_id: Optional[str] = None
) -> Dict[str, Any]:
    """Convert Gemini response to Anthropic format (non-streaming)."""

    if not request_id:
        request_id = f"msg_{uuid.uuid4().hex[:24]}"

    anthropic_response = {
        "id": request_id,
        "type": "message",
        "role": "assistant",
        "model": "",
        "content": [],
        "stop_reason": None,
        "stop_sequence": None,
        "usage": {
            "input_tokens": 0,
            "output_tokens": 0
        }
    }

    # Extract model version
    if "modelVersion" in gemini_response:
        anthropic_response["model"] = gemini_response["modelVersion"]

    # Process candidates (Gemini can have multiple, we take the first)
    candidates = gemini_response.get("candidates", [])
    if candidates:
        candidate = candidates[0]

        # Extract finish reason
        finish_reason = candidate.get("finishReason")
        if finish_reason:
            # Map Gemini finish reasons to Anthropic
            reason_map = {
                "STOP": "end_turn",
                "MAX_TOKENS": "max_tokens",
                "SAFETY": "stop_sequence",
                "RECITATION": "stop_sequence",
                "LANGUAGE": "stop_sequence",
                "OTHER": "stop_sequence"
            }
            anthropic_response["stop_reason"] = reason_map.get(finish_reason, "end_turn")

        # Process content parts
        content = candidate.get("content", {})
        parts = content.get("parts", [])

        for part in parts:
            # Check for thinking content first (parts with thought=true also have text)
            if part.get("thought", False) and "text" in part:
                # Thinking/reasoning content - convert to Anthropic thinking block
                anthropic_response["content"].append({
                    "type": "thinking",
                    "thinking": part["text"],
                    "signature": ""
                })
            elif "text" in part:
                # Regular text content
                anthropic_response["content"].append({
                    "type": "text",
                    "text": part["text"]
                })
            elif "functionCall" in part:
                # Convert functionCall to tool_use
                func_call = part["functionCall"]
                tool_use = {
                    "type": "tool_use",
                    "id": f"toolu_{uuid.uuid4().hex[:24]}",
                    "name": func_call.get("name", ""),
                    "input": func_call.get("args", {})
                }
                anthropic_response["content"].append(tool_use)

    # Extract usage metadata
    usage_metadata = gemini_response.get("usageMetadata", {})
    if usage_metadata:
        anthropic_response["usage"]["input_tokens"] = usage_metadata.get("promptTokenCount", 0)
        anthropic_response["usage"]["output_tokens"] = usage_metadata.get("candidatesTokenCount", 0)

        # Add thinking/reasoning tokens if present
        if "thoughtsTokenCount" in usage_metadata:
            anthropic_response["usage"]["thinking_tokens"] = usage_metadata["thoughtsTokenCount"]

        # Add cache-related tokens if present
        if "cachedContentTokenCount" in usage_metadata:
            # Gemini's cachedContentTokenCount maps to cache_read_input_tokens
            anthropic_response["usage"]["cache_read_input_tokens"] = usage_metadata["cachedContentTokenCount"]

    return anthropic_response


def gemini_response_to_anthropic_streaming(
    line: str,
    context: Optional[Dict[str, Any]] = None
) -> List[Dict[str, Any]]:
    """Convert Gemini streaming response line to Anthropic SSE events.

    Returns a list of event dictionaries to be sent as SSE.
    """

    if context is None:
        context = {}

    events = []

    # Skip [DONE] markers (with or without "data: " prefix)
    if line.strip() == "[DONE]" or line.strip() == "data: [DONE]":
        return events

    # Parse the Gemini SSE line
    if not line.startswith("data: "):
        return events

    try:
        data = json.loads(line[6:])  # Skip "data: " prefix
    except json.JSONDecodeError:
        return events

    # Initialize context if needed
    if "message_id" not in context:
        context["message_id"] = f"msg_{uuid.uuid4().hex[:24]}"
        context["content_index"] = 0
        context["tool_use_id"] = None
        context["tool_name"] = None

        # Send message_start event
        events.append({
            "event": "message_start",
            "data": {
                "type": "message_start",
                "message": {
                    "id": context["message_id"],
                    "type": "message",
                    "role": "assistant",
                    "model": data.get("modelVersion", ""),
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 0, "output_tokens": 0}
                }
            }
        })

    # Process candidates
    candidates = data.get("candidates", [])
    if candidates:
        candidate = candidates[0]
        content = candidate.get("content", {})
        parts = content.get("parts", [])

        for part in parts:
            # Check if this is a thinking/reasoning part (has "thought": true)
            is_thought = part.get("thought", False)

            if "text" in part and part["text"]:
                # Determine if this is thinking content or regular text
                if is_thought:
                    # Thinking/reasoning content
                    if context.get("current_type") != "thinking":
                        # Start new thinking block
                        events.append({
                            "event": "content_block_start",
                            "data": {
                                "type": "content_block_start",
                                "index": context["content_index"],
                                "content_block": {"type": "thinking", "thinking": "", "signature": ""}
                            }
                        })
                        context["current_type"] = "thinking"

                    # Send thinking delta
                    events.append({
                        "event": "content_block_delta",
                        "data": {
                            "type": "content_block_delta",
                            "index": context["content_index"],
                            "delta": {"type": "thinking_delta", "thinking": part["text"]}
                        }
                    })
                else:
                    # Regular text content
                    if context.get("current_type") != "text":
                        # Start new text block
                        events.append({
                            "event": "content_block_start",
                            "data": {
                                "type": "content_block_start",
                                "index": context["content_index"],
                                "content_block": {"type": "text", "text": ""}
                            }
                        })
                        context["current_type"] = "text"

                    # Send text delta
                    events.append({
                        "event": "content_block_delta",
                        "data": {
                            "type": "content_block_delta",
                            "index": context["content_index"],
                            "delta": {"type": "text_delta", "text": part["text"]}
                        }
                    })

            elif "functionCall" in part:
                # Function call
                func_call = part["functionCall"]

                # End previous block if needed
                if context.get("current_type"):
                    events.append({
                        "event": "content_block_stop",
                        "data": {
                            "type": "content_block_stop",
                            "index": context["content_index"]
                        }
                    })
                    context["content_index"] += 1

                # Start tool use block
                tool_id = f"toolu_{uuid.uuid4().hex[:24]}"
                events.append({
                    "event": "content_block_start",
                    "data": {
                        "type": "content_block_start",
                        "index": context["content_index"],
                        "content_block": {
                            "type": "tool_use",
                            "id": tool_id,
                            "name": func_call.get("name", ""),
                            "input": {}
                        }
                    }
                })

                # Send tool input
                if "args" in func_call:
                    events.append({
                        "event": "content_block_delta",
                        "data": {
                            "type": "content_block_delta",
                            "index": context["content_index"],
                            "delta": {
                                "type": "input_json_delta",
                                "partial_json": json.dumps(func_call["args"])
                            }
                        }
                    })

                context["current_type"] = "tool_use"

        # Check for finish reason
        finish_reason = candidate.get("finishReason")
        if finish_reason:
            # End current block
            if context.get("current_type"):
                events.append({
                    "event": "content_block_stop",
                    "data": {
                        "type": "content_block_stop",
                        "index": context["content_index"]
                    }
                })

            # Send message delta with stop reason
            reason_map = {
                "STOP": "end_turn",
                "MAX_TOKENS": "max_tokens",
                "SAFETY": "stop_sequence",
                "RECITATION": "stop_sequence",
                "LANGUAGE": "stop_sequence",
                "OTHER": "stop_sequence"
            }

            # Build usage metadata for message_delta
            usage_metadata = data.get("usageMetadata", {})
            usage = {"output_tokens": usage_metadata.get("candidatesTokenCount", 0)}

            # Add thinking tokens if present
            if "thoughtsTokenCount" in usage_metadata:
                usage["thinking_tokens"] = usage_metadata["thoughtsTokenCount"]

            # Add cache tokens if present
            if "cachedContentTokenCount" in usage_metadata:
                usage["cache_read_input_tokens"] = usage_metadata["cachedContentTokenCount"]

            events.append({
                "event": "message_delta",
                "data": {
                    "type": "message_delta",
                    "delta": {"stop_reason": reason_map.get(finish_reason, "end_turn")},
                    "usage": usage
                }
            })

            # Send message stop
            events.append({
                "event": "message_stop",
                "data": {"type": "message_stop"}
            })

    return events


def gemini_token_count_response(total_tokens: int) -> Dict[str, Any]:
    """Convert token count to Gemini format.

    Returns Gemini-compatible token count response matching CLIProxyAPI format.
    """
    return {
        "totalTokens": total_tokens,
        "promptTokensDetails": [
            {"modality": "TEXT", "tokenCount": total_tokens}
        ]
    }


__all__ = [
    "anthropic_request_to_gemini",
    "gemini_response_to_anthropic",
    "gemini_response_to_anthropic_streaming",
    "gemini_token_count_response",
]