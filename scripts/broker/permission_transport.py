"""
Control message transport for intercepting Claude CLI control channel.

This transport intercepts control_request messages and routes them to
registered handlers. Handlers include permission management, hooks, etc.
"""
import json
import logging
import os
from typing import Any, Optional, AsyncIterator
from pathlib import Path

from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport
from claude_agent_sdk.types import ClaudeAgentOptions
from claude_agent_sdk._errors import CLIConnectionError

log = logging.getLogger(__name__)


class PermissionTransport(SubprocessCLITransport):
    """
    Custom transport that intercepts control_request messages for permission handling.
    
    This transport extends the SDK's SubprocessCLITransport to intercept control_request
    messages before they reach the message parser, allowing custom permission handling
    with runtime-modifiable behavior.
    """
    
    def __init__(
        self,
        permission_manager,
        tab_id: str,
        prompt: str | AsyncIterator[dict[str, Any]],
        options: ClaudeAgentOptions,
        cli_path: str | Path | None = None,
    ):
        """
        Initialize the permission transport.

        Args:
            permission_manager: Runtime permission manager for handling requests
            tab_id: Tab ID for iOS routing
            prompt: Initial prompt or message stream
            options: Claude Agent options
            cli_path: Optional path to Claude CLI
        """
        log.info(f"PermissionTransport initialized with permission_manager={permission_manager}, tab_id={tab_id}")
        super().__init__(prompt, options, cli_path)
        self.permission_manager = permission_manager
        self.tab_id = tab_id
        self._intercepted_count = 0
        self._cli_to_broker_request_map = {}  # Map CLI request_id -> broker request_id
    
    def read_messages(self) -> AsyncIterator[dict[str, Any]]:
        """
        Read messages from the subprocess, intercepting control_request messages.

        Yields:
            Messages from Claude CLI, excluding intercepted control_request messages
        """
        return self._read_with_interception()

    async def _read_with_interception(self) -> AsyncIterator[dict[str, Any]]:
        """Internal implementation with control message interception."""
        log.info("PermissionTransport: Starting message interception")
        async for data in super().read_messages():
            msg_type = data.get("type")

            # Always log control messages for debugging
            if msg_type == "control_request":
                log.info(f"[CONTROL_REQUEST →] {json.dumps(data, indent=2)}")

                # Only intercept can_use_tool requests - let other control requests pass through to CLI
                request = data.get("request", {})
                subtype = request.get("subtype")

                if subtype == "can_use_tool":
                    # Handle the permission request
                    await self._handle_control_request(data)
                    # Don't yield to the client - we've handled it
                    continue
                else:
                    # Pass through to CLI (set_permission_mode, set_model, interrupt, etc.)
                    log.info(f"Passing through control_request subtype '{subtype}' to CLI")
                    yield data
                    continue

            # Log control_response messages
            elif msg_type == "control_response":
                log.info(f"[CONTROL_RESPONSE ←] {json.dumps(data, indent=2)}")

            # Pass through all other messages
            yield data
    
    async def _handle_control_request(self, data: dict[str, Any]) -> None:
        """
        Handle a can_use_tool control_request message for tool permissions.

        Args:
            data: The control_request message data (should have subtype: can_use_tool)
        """
        import uuid

        request = data.get("request", {})
        cli_request_id = data.get("request_id")  # CLI's request_id

        if not cli_request_id:
            log.error("Control request missing request_id")
            return

        # Handle tool permission request
        tool_name = request.get("tool_name")
        tool_input = request.get("input", {})

        # Generate broker request_id with tab_id prefix for iOS routing
        broker_request_id = f"{self.tab_id}:{uuid.uuid4().hex[:8]}"
        self._cli_to_broker_request_map[cli_request_id] = broker_request_id

        log.info(f"Intercepted permission request for tool '{tool_name}' (cli_req_id={cli_request_id}, broker_req_id={broker_request_id})")
        self._intercepted_count += 1

        try:
            # Get permission decision from manager (runtime state!)
            log.info(f"Calling permission_manager.get_permission for {tool_name}")
            decision = await self.permission_manager.get_permission(
                tool_name=tool_name,
                tool_input=tool_input,
                request_id=broker_request_id  # Use broker request_id with tab_id
            )

            log.info(f"Permission decision for '{tool_name}': {decision}")

            # Build control_response using CLI's original request_id
            control_response = {
                "type": "control_response",
                "response": {
                    "subtype": "success",
                    "request_id": cli_request_id,  # Use CLI's request_id
                    "response": decision
                }
            }

        except Exception as e:
            log.error(f"Permission manager error: {e}")

            # Send error response
            control_response = {
                "type": "control_response",
                "response": {
                    "subtype": "error",
                    "request_id": cli_request_id,  # Use CLI's request_id
                    "error": str(e)
                }
            }

        # Send control_response back to CLI
        await self._send_control_response(control_response)

        # Clean up mapping
        self._cli_to_broker_request_map.pop(cli_request_id, None)
    
    async def _send_control_response(self, response: dict[str, Any]) -> None:
        """
        Send a control_response message to the CLI subprocess.

        Args:
            response: The control_response message to send
        """
        try:
            log.info(f"[CONTROL_RESPONSE →CLI] {json.dumps(response, indent=2)}")

            message = json.dumps(response) + "\n"
            log.info(f"About to send control_response via self.write()")
            await self.write(message)
            log.info(f"Successfully sent control_response: {response['response']['request_id']}")
        except Exception as e:
            log.error(f"Failed to send control_response: {e}", exc_info=True)
            raise
    
    def get_interception_stats(self) -> dict[str, Any]:
        """
        Get statistics about intercepted control requests.
        
        Returns:
            Dictionary with interception statistics
        """
        return {
            "intercepted_count": self._intercepted_count,
            "manager_mode": getattr(self.permission_manager, "permission_mode", "unknown")
        }