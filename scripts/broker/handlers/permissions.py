"""
Permission handler for managing permission flows.
"""
import logging
from typing import Dict, Any
from websockets import WebSocketServerProtocol

from .base import BaseHandler
from ..models import ErrorCode

log = logging.getLogger(__name__)


class PermissionHandler(BaseHandler):
    """Handles permission-related messages."""

    async def handle_permission_response(self, data: dict, ws: WebSocketServerProtocol):
        """Handle permission response. Extracts tabId from message."""
        # Extract session from tabId
        session = await self._get_session_from_message(data, ws)
        if not session:
            return

        request_id = data.get('requestId')
        decision = data.get('decision')

        if not request_id or not decision:
            log.warning(f"Incomplete permission response: requestId={request_id}, decision={decision}")
            return

        log.info(f"Received permission response from iOS: requestId={request_id}, decision={decision}")

        # Send ACK if iOS provided seq
        ios_seq = data.get('seq')
        if ios_seq is not None:
            # Use tab_id directly for ACK tracking (consistent across all message types)
            ready_messages = await self.ack_manager.process_ios_message(session.tab_id, ios_seq, message_data=None)
            for broker_ack_seq, is_duplicate in ready_messages:
                ack_seq = await self.ack_manager.get_next_broker_seq(session.tab_id)
                await self._send(ws, {
                    'type': 'message_received_ack',
                    'tabId': session.tab_id,
                    'ack_seq': broker_ack_seq,
                    'seq': ack_seq,
                    'is_duplicate': is_duplicate
                })
                log.info(f"Sent ACK for permission_response: ios_seq={ios_seq}, broker_ack_seq={broker_ack_seq}")

            if not ready_messages:
                log.info(f"PERMISSION_RESPONSE seq={ios_seq} buffered - waiting for earlier messages")

        try:
            # Handle "auto" behavior from iOS
            # "auto" = Convert to "allow" immediately, then set mode after resolving
            should_set_mode = False
            if decision.get("behavior") == "auto":
                log.info(f"Auto-accept requested for {request_id} - will set permission mode after resolving")
                # Convert "auto" to "allow" for the permission system
                decision["behavior"] = "allow"
                log.info(f"Converted 'auto' to 'allow' for permission {request_id}")
                should_set_mode = True

            # Forward to Claude interface (must complete before set_permission_mode)
            await self.claude_interface.resolve_permission(session.session_id, request_id, decision)
            log.info(f"Successfully resolved permission {request_id} with behavior={decision.get('behavior')}")

            # Now set permission mode if auto was requested
            # This happens AFTER permission is resolved to avoid deadlock
            if should_set_mode:
                claude_session = self.claude_interface.get_session(session.session_id)
                if claude_session and claude_session.client:
                    log.info(f"Setting permission mode to 'acceptEdits' for session {session.session_id}")
                    try:
                        await claude_session.client.set_permission_mode("acceptEdits")
                        log.info(f"Successfully set permission mode to 'acceptEdits'")

                        # Update session state for tracking
                        async with self.session_manager._lock:
                            session.permission_mode = "acceptEdits"

                        # Notify iOS that mode was changed
                        broker_seq = await self.ack_manager.get_next_broker_seq(session.tab_id)
                        await self._send(ws, {
                            'type': 'permission_mode_updated',
                            'tabId': session.tab_id,
                            'mode': 'acceptEdits',
                            'status': 'success',
                            'seq': broker_seq
                        })
                        log.info(f"Sent permission_mode_updated to iOS for auto-accept")
                    except Exception as mode_error:
                        log.error(f"Failed to set permission mode: {mode_error}", exc_info=True)
                else:
                    log.warning(f"Cannot set permission mode: no Claude session/client available")
        except Exception as e:
            log.error(f"Failed to resolve permission for session {session.session_id}: {e}", exc_info=True)

    async def handle_set_permission_mode(self, data: dict, ws: WebSocketServerProtocol):
        """Handle permission mode change request from iOS. Extracts tabId from message."""
        # Extract session from tabId
        session = await self._get_session_from_message(data, ws)
        if not session:
            return

        mode = data.get('mode')
        if not mode:
            await self._send_error(ws, "Missing 'mode' in set_permission_mode request", session.tab_id, ErrorCode.SYSTEM_ERROR)
            return

        # Process iOS message with sequential ordering
        ios_seq = data.get('seq')
        ready_messages = await self.ack_manager.process_ios_message(session.tab_id, ios_seq, message_data=None)

        # Send ACKs for all ready messages in order
        for broker_ack_seq, is_duplicate in ready_messages:
            ack_seq = await self.ack_manager.get_next_broker_seq(session.tab_id)
            await self._send(ws, {
                'type': 'message_received_ack',
                'tabId': session.tab_id,
                'ack_seq': broker_ack_seq,
                'seq': ack_seq,
                'is_duplicate': is_duplicate
            })
            log.debug(f"Sent ACK for SET_PERMISSION_MODE seq={broker_ack_seq} (duplicate={is_duplicate})")

        # If no messages ready (buffered) or duplicate, don't process further
        if not ready_messages:
            log.info(f"SET_PERMISSION_MODE seq={ios_seq} buffered - waiting for earlier messages")
            return

        if ready_messages[0][1]:  # First message is duplicate
            log.info(f"Ignoring duplicate SET_PERMISSION_MODE seq={ios_seq} from iOS")
            return

        # Validate mode
        valid_modes = ['default', 'acceptEdits', 'plan', 'bypassPermissions']
        if mode not in valid_modes:
            await self._send_error(ws, f"Invalid permission mode '{mode}'. Valid modes: {valid_modes}", session.tab_id, ErrorCode.SYSTEM_ERROR)
            return

        # Get Claude session and set permission mode
        claude_session = self.claude_interface.get_session(session.session_id)
        if claude_session and claude_session.client:
            try:
                log.info(f"[PERMISSION DEBUG] Starting set_permission_mode('{mode}') for session {session.session_id}")
                log.info(f"[PERMISSION DEBUG] iOS seq={ios_seq}, will send to Claude CLI now...")

                await claude_session.client.set_permission_mode(mode)

                log.info(f"[PERMISSION DEBUG] set_permission_mode('{mode}') completed for session {session.session_id}")
                log.info(f"[PERMISSION DEBUG] Note: This only means command was sent to CLI, not that CLI has applied it")

                # Update session state for tracking
                async with self.session_manager._lock:
                    session.permission_mode = mode

                # Acknowledge to iOS with sequence number
                broker_seq = await self.ack_manager.get_next_broker_seq(session.tab_id)
                await self._send(ws, {
                    'type': 'permission_mode_updated',
                    'tabId': session.tab_id,
                    'mode': mode,
                    'status': 'success',
                    'seq': broker_seq
                })
            except Exception as e:
                log.error(f"Failed to set permission mode for session {session.session_id}: {e}")
                await self._send_error(ws, f"Permission mode change failed: {e}", session.tab_id, ErrorCode.SYSTEM_ERROR)
        else:
            log.warning(f"No active Claude client for session {session.session_id}")
            await self._send_error(ws, "No active Claude session to change permission mode", session.tab_id, ErrorCode.SESSION_NOT_FOUND)

    async def send_permission_request_to_ios(
        self,
        tool_name: str,
        tool_input: Dict[str, Any],
        request_id: str
    ):
        """
        Send permission request to iOS client for a tab.
        Request is buffered for replay in case iOS disconnects before responding.

        Tab ID is extracted from request_id (format: {tab_id}:{unique_id})

        Args:
            tool_name: Name of the tool requesting permission
            tool_input: Tool input parameters
            request_id: Unique request identifier (contains tab_id for routing)
        """
        # Extract tab_id from request_id (format: tab_id:unique_id)
        try:
            tab_id, _ = request_id.split(':', 1)
        except ValueError:
            log.error(f"Invalid request_id format (expected tab_id:unique_id): {request_id}")
            return

        # Look up session by tab_id
        session = await self.session_manager.get_session_by_tab(tab_id)
        if not session:
            log.warning(f"No session found for tab {tab_id}")
            return

        # Get all connections for this session
        connections = await self.connection_manager.get_session_connections(session.session_id)

        if not connections:
            log.warning(f"No active iOS connections for tab {tab_id}")
            return

        # Build permission request message
        permission_msg = {
            'type': 'permission_request',
            'tabId': tab_id,  # iOS only sees tabId
            'requestId': request_id,
            'toolName': tool_name,
            'toolInput': tool_input
        }

        # Buffer the message for replay (critical for disconnect/reconnect)
        # This ensures permission requests are replayed if iOS reconnects
        buffered_msg = await self.message_buffer.add_message(
            session_id=session.session_id,
            content=permission_msg
        )

        # Add sequence number for ACK tracking
        permission_msg['seq'] = buffered_msg.seq

        # Send to all active connections
        for conn in connections:
            try:
                await self._send(conn.websocket, permission_msg)
                log.info(f"Sent permission request {request_id} for {tool_name} to iOS tab {tab_id} (seq={buffered_msg.seq})")
            except Exception as e:
                log.error(f"Failed to send permission request to connection {conn.connection_id}: {e}")
