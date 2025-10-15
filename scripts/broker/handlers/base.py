"""
Base handler with shared utilities for all message handlers.
"""
import json
import logging
from typing import Optional
from websockets import WebSocketServerProtocol

from ..core.session_manager import SessionManager
from ..core.connection_manager import ConnectionManager
from ..core.message_buffer import MessageBuffer
from ..core.ack_manager import AckManager
from ..routes import RouteManager
from ..claude_interface import ClaudeInterface
from ..models import SessionInfo, ErrorCode

log = logging.getLogger(__name__)


class BaseHandler:
    """Base class for all message handlers with shared utilities."""

    def __init__(
        self,
        broker,  # Broker instance for global credential access
        session_manager: SessionManager,
        connection_manager: ConnectionManager,
        message_buffer: MessageBuffer,
        route_manager: RouteManager,
        claude_interface: ClaudeInterface,
        ack_manager: Optional[AckManager] = None
    ):
        """
        Initialize base handler.

        Args:
            broker: Broker instance for global credential access
            session_manager: Session management instance
            connection_manager: Connection management instance
            message_buffer: Message buffer instance
            route_manager: Route management instance
            claude_interface: Claude interface instance
            ack_manager: ACK management instance (optional, will create if not provided)
        """
        self.broker = broker
        self.session_manager = session_manager
        self.connection_manager = connection_manager
        self.message_buffer = message_buffer
        self.route_manager = route_manager
        self.claude_interface = claude_interface
        self.ack_manager = ack_manager or AckManager()

    async def _get_session_from_message(self, data: dict, ws: WebSocketServerProtocol) -> Optional[SessionInfo]:
        """
        Extract tabId from message and look up session.

        Args:
            data: Message data containing tabId
            ws: WebSocket for error responses

        Returns:
            SessionInfo or None if error (error already sent to WS)
        """
        tab_id = data.get('tabId')
        if not tab_id:
            await self._send_error(ws, "Missing tabId in message", None, ErrorCode.MISSING_TAB_ID)
            return None

        session = await self.session_manager.get_session_by_tab(tab_id)
        if not session:
            await self._send_error(ws, f"No session found for tabId: {tab_id}", tab_id, ErrorCode.SESSION_NOT_FOUND)
            return None

        return session

    async def _send(self, ws: WebSocketServerProtocol, data: dict):
        """Send data to WebSocket."""
        try:
            await ws.send(json.dumps(data))
        except Exception as e:
            log.error(f"Failed to send to WebSocket: {e}")

    async def _send_error(self, ws: WebSocketServerProtocol, error: str, tab_id: str = None, error_code: str = None):
        """Send error message to WebSocket (iOS only sees tabId)."""
        error_msg = {
            'type': 'error',
            'error': error,
            'tabId': tab_id  # iOS only sees tabId, never sessionId
        }
        if error_code:
            error_msg['errorCode'] = error_code

        # Add sequence number if we have a tab_id
        if tab_id:
            # Try to get session for proper seq tracking
            session = await self.session_manager.get_session_by_tab(tab_id)
            if session:
                error_seq = await self.ack_manager.get_next_broker_seq(session.session_id)
                error_msg['seq'] = error_seq

        await self._send(ws, error_msg)
