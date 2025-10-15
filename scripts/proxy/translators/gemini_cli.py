"""Translator for Gemini CLI/Cloud Code Assist API format.

This module handles conversion between Anthropic (Claude) message format
and Google's Gemini CLI API format used by Cloud Code Assist.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

# Reuse most functionality from standard Gemini translator
from .gemini import (
    anthropic_request_to_gemini,
    gemini_response_to_anthropic,
    gemini_response_to_anthropic_streaming,
)
from .. import logging_control


def anthropic_request_to_gemini_cli(
    request_body: Dict[str, Any],
    model: str,
    system_instruction: Optional[str] = None,
    project_id: Optional[str] = None,
    reasoning_level: Optional[str] = None,
) -> Dict[str, Any]:
    """Convert Anthropic request format to Gemini CLI format.

    Gemini CLI wraps the standard Gemini format in a 'request' field
    and adds model/project fields at the top level.

    Key difference: Gemini CLI uses 'systemInstruction' (camelCase)
    instead of 'system_instruction' (snake_case).
    """
    # Use standard Gemini conversion (with reasoning level)
    gemini_request = anthropic_request_to_gemini(
        request_body, model, system_instruction, reasoning_level
    )

    # Convert system_instruction to systemInstruction for CLI format
    if "system_instruction" in gemini_request:
        gemini_request["systemInstruction"] = gemini_request.pop("system_instruction")

    # Wrap in the Gemini CLI format
    gemini_cli_body = {
        "request": gemini_request,
        "model": model,
    }

    # Add project ID if provided (for Cloud Code Assist)
    if project_id:
        if logging_control.is_enabled():
            print(f"[DEBUG] Adding project_id to request: {project_id}")
        gemini_cli_body["project"] = project_id
    else:
        if logging_control.is_enabled():
            print("[DEBUG] No project_id provided for Gemini CLI request")

    return gemini_cli_body


def gemini_cli_response_to_anthropic(
    gemini_cli_response: Dict[str, Any],
    request_id: Optional[str] = None
) -> Dict[str, Any]:
    """Convert Gemini CLI response to Anthropic format.

    Gemini CLI wraps the response in a "response" field, so we need to
    unwrap it before passing to the standard Gemini translator.
    """
    # Extract the inner response object
    if "response" in gemini_cli_response:
        inner_response = gemini_cli_response["response"]
    else:
        # Fallback if no wrapper (shouldn't happen with real Gemini CLI)
        inner_response = gemini_cli_response

    # Use the standard Gemini response translator on the unwrapped response
    return gemini_response_to_anthropic(inner_response, request_id)


def gemini_cli_response_to_anthropic_streaming(
    line: str,
    context: Optional[Dict[str, Any]] = None
) -> List[Dict[str, Any]]:
    """Convert Gemini CLI streaming response to Anthropic SSE events.

    Gemini CLI streaming also wraps responses, so unwrap before processing.
    """
    import json

    # Skip [DONE] markers (with or without "data: " prefix)
    if line.strip() == "[DONE]" or line.strip() == "data: [DONE]":
        return []

    if not line.startswith("data: "):
        return []

    try:
        data = json.loads(line[6:])  # Skip "data: " prefix
    except json.JSONDecodeError:
        return []

    # Unwrap the Gemini CLI response wrapper
    if "response" in data:
        unwrapped_data = data["response"]
    else:
        unwrapped_data = data

    # Convert back to the expected line format for standard Gemini streaming
    unwrapped_line = f"data: {json.dumps(unwrapped_data)}"

    # Use the standard Gemini streaming translator
    return gemini_response_to_anthropic_streaming(unwrapped_line, context)


def gemini_cli_token_count_response(total_tokens: int) -> Dict[str, Any]:
    """Convert token count to Gemini CLI format.

    Gemini CLI uses the same format as standard Gemini for token counts.
    """
    from .gemini import gemini_token_count_response
    return gemini_token_count_response(total_tokens)


__all__ = [
    "anthropic_request_to_gemini_cli",
    "gemini_cli_response_to_anthropic",
    "gemini_cli_response_to_anthropic_streaming",
    "gemini_cli_token_count_response",
]