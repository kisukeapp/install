"""Shared context for managing state across translation and execution.

Following CLIProxyAPI's pattern of separation of concerns.
"""

from __future__ import annotations

import secrets
import string
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


# Anthropic tool ID format
_TOOL_ID_ALPHABET = string.ascii_letters + string.digits


def generate_tool_id() -> str:
    """Generate Anthropic-format tool ID: toolu_<24-char-alphanum>."""
    return "toolu_" + "".join(secrets.choice(_TOOL_ID_ALPHABET) for _ in range(24))


@dataclass
class ToolMapping:
    """Maps between external (OpenAI/Codex) and Anthropic tool identifiers."""

    external_id: str
    anthropic_id: str
    name: str

    # For Codex: track shortened names
    short_name: Optional[str] = None


@dataclass
class ToolContext:
    """Manages tool ID/name mappings between protocols."""

    # External ID -> ToolMapping
    mappings: Dict[str, ToolMapping] = field(default_factory=dict)

    # Anthropic ID -> External ID (reverse lookup)
    reverse_ids: Dict[str, str] = field(default_factory=dict)

    # Name -> External ID (for lookups by name)
    name_to_id: Dict[str, str] = field(default_factory=dict)

    def register_tool(self, external_id: str, name: str, short_name: Optional[str] = None) -> str:
        """Register a tool and return its Anthropic ID."""
        if external_id in self.mappings:
            return self.mappings[external_id].anthropic_id

        anthropic_id = generate_tool_id()
        mapping = ToolMapping(
            external_id=external_id,
            anthropic_id=anthropic_id,
            name=name,
            short_name=short_name
        )

        self.mappings[external_id] = mapping
        self.reverse_ids[anthropic_id] = external_id
        self.name_to_id[name] = external_id

        return anthropic_id

    def get_anthropic_id(self, external_id: str) -> str:
        """Get Anthropic ID for external ID."""
        if external_id in self.mappings:
            return self.mappings[external_id].anthropic_id
        # Generate new mapping
        return self.register_tool(external_id, "function")

    def get_external_id(self, anthropic_id: str) -> Optional[str]:
        """Get external ID from Anthropic ID."""
        return self.reverse_ids.get(anthropic_id)

    def get_tool_name(self, tool_id: str) -> str:
        """Get tool name by any ID (external or Anthropic)."""
        # Try as external ID
        if tool_id in self.mappings:
            return self.mappings[tool_id].name
        # Try as Anthropic ID
        external_id = self.reverse_ids.get(tool_id)
        if external_id and external_id in self.mappings:
            return self.mappings[external_id].name
        return "function"

    def get_short_name_by_name(self, name: str) -> Optional[str]:
        """Get shortened tool name by original name."""
        external_id = self.name_to_id.get(name)
        if external_id and external_id in self.mappings:
            mapping = self.mappings[external_id]
            return mapping.short_name or mapping.name
        return None


@dataclass
class StreamingState:
    """Manages state for streaming responses."""

    # Content block indices
    next_index: int = 0
    text_index: Optional[int] = None

    # Block states
    text_started: bool = False
    thinking_started: Dict[int, bool] = field(default_factory=dict)

    # Tool accumulation (index -> state)
    tool_states: Dict[int, Dict[str, Any]] = field(default_factory=dict)

    # Usage tracking
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None

    # Completion
    finish_reason: Optional[str] = None

    def allocate_index(self) -> int:
        """Allocate next content block index."""
        idx = self.next_index
        self.next_index += 1
        return idx

    def get_text_index(self) -> int:
        """Get or allocate text content index."""
        if self.text_index is None:
            self.text_index = self.allocate_index()
        return self.text_index


@dataclass
class TranslationContext:
    """Context shared across translation pipeline.

    Inspired by CLIProxyAPI's pipeline context pattern.
    """

    # Protocol information
    source_protocol: str  # "anthropic"
    target_protocol: str  # "codex" or "openai_v1"

    # Model information
    requested_model: str
    effective_model: Optional[str] = None

    # Tool mappings
    tools: ToolContext = field(default_factory=ToolContext)

    # Streaming state
    streaming: StreamingState = field(default_factory=StreamingState)

    # Translation parameters (like CLIProxyAPI's param *any)
    param: Any = None

    # Request tracking
    original_request: Optional[bytes] = None
    translated_request: Optional[bytes] = None

    def reset_streaming(self) -> None:
        """Reset streaming state for new response."""
        self.streaming = StreamingState()

    @classmethod
    def for_codex(cls, model: str) -> TranslationContext:
        """Create context for Anthropic → Codex translation."""
        return cls(
            source_protocol="anthropic",
            target_protocol="codex",
            requested_model=model
        )

    @classmethod
    def for_openai_v1(cls, model: str) -> TranslationContext:
        """Create context for Anthropic → OpenAI v1 translation."""
        return cls(
            source_protocol="anthropic",
            target_protocol="openai_v1",
            requested_model=model
        )