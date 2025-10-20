"""
ACK handler for acknowledgment handling.
"""
import logging
from websockets import WebSocketServerProtocol

from .base import BaseHandler

log = logging.getLogger(__name__)


class AckHandler(BaseHandler):
    """Handles acknowledgment messages."""

    async def handle_response_ack(self, data: dict, ws: WebSocketServerProtocol):
        """Handle response acknowledgment from iOS. Extracts tabId from message."""
        # Extract session from tabId
        session = await self._get_session_from_message(data, ws)
        if not session:
            return

        seq = data.get('seq')
        if seq is not None:
            # Acknowledge messages cumulatively in buffer (buffer keyed by session_id)
            await self.session_manager.acknowledge_up_to(session.session_id, seq)
            # Also update ACK manager using tabId namespace
            await self.ack_manager.ack_from_ios(session.tab_id, seq)
            log.debug(f"iOS acknowledged up to seq={seq} for tab {session.tab_id} (session {session.session_id})")
