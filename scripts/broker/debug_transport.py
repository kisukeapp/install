"""
Debug transport for logging all control channel messages.

This transport logs all control_request and control_response messages
for debugging purposes, then passes them through to the SDK unchanged.
"""
import json
import logging
from typing import Any, AsyncIterator
from pathlib import Path

from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport
from claude_agent_sdk.types import ClaudeAgentOptions

log = logging.getLogger(__name__)


class DebugTransport(SubprocessCLITransport):
    """
    Debug transport that logs all control channel messages.

    This is used for debugging to see what control messages flow between
    the SDK and Claude CLI process.
    """

    def __init__(
        self,
        prompt: str | Any,
        options: ClaudeAgentOptions,
        cli_path: str | Path | None = None,
        log_all_messages: bool = False
    ):
        """
        Initialize debug transport.

        Args:
            prompt: Prompt or message stream
            options: Claude options
            cli_path: Path to Claude CLI
            log_all_messages: If True, log ALL messages (not just control). Default False.
        """
        super().__init__(prompt, options, cli_path)
        self.log_all_messages = log_all_messages

    async def send_message(self, message: dict[str, Any]) -> None:
        """Send message and log if it's a control_response."""
        msg_type = message.get("type")

        if msg_type == "control_response":
            log.info(f"[CONTROL_RESPONSE →CLI] {json.dumps(message, indent=2)}")
        elif self.log_all_messages:
            msg_str = json.dumps(message)
            if len(msg_str) > 500:
                msg_str = msg_str[:500] + "..."
            log.debug(f"[SDK_SEND] type={msg_type}: {msg_str}")

        await super().send_message(message)

    def read_messages(self) -> AsyncIterator[dict[str, Any]]:
        """Read messages and log control channel traffic."""
        return self._debug_read_messages()

    async def _debug_read_messages(self) -> AsyncIterator[dict[str, Any]]:
        """Internal implementation with control message logging."""
        async for raw_message in super().read_messages():
            msg_type = raw_message.get("type")

            # Log control requests (CLI → SDK)
            if msg_type == "control_request":
                log.info(f"[CONTROL_REQUEST →] {json.dumps(raw_message, indent=2)}")

            # Log control responses (SDK → CLI)
            elif msg_type == "control_response":
                log.info(f"[CONTROL_RESPONSE ←] {json.dumps(raw_message, indent=2)}")

            # Optionally log all other messages
            elif self.log_all_messages:
                # Truncate large content for readability
                msg_str = json.dumps(raw_message)
                if len(msg_str) > 500:
                    msg_str = msg_str[:500] + "..."
                log.debug(f"[SDK_MESSAGE] type={msg_type}: {msg_str}")

            # Pass through to SDK unchanged
            yield raw_message
