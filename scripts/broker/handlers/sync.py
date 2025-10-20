"""
Sync handler to trigger broker→iOS replay for gaps while still connected.
"""
import logging
from websockets import WebSocketServerProtocol

from .base import BaseHandler

log = logging.getLogger(__name__)


class SyncHandler(BaseHandler):
    """Handles SYNC requests from iOS to maintain synchronicity."""

    async def handle_sync(self, data: dict, ws: WebSocketServerProtocol):
        """Handle client sync request.

        Expected payload:
            {
              "type": "sync",
              "tabId": "...",
              "last_received_seq": <int|null>
            }
        """
        # Resolve session from tabId
        last_received_seq = data.get('last_received_seq')
        log.info(f"SYNC requested: tabId={data.get('tabId')} last_received_seq={last_received_seq}")
        session = await self._get_session_from_message(data, ws)
        if not session:
            return

        # Identify the current connection id for this websocket
        connections = await self.connection_manager.get_session_connections(session.session_id)
        connection_id = None
        for conn in connections:
            if conn.websocket is ws:
                connection_id = conn.connection_id
                break

        # Fallback: use first active connection if exact match not found
        if not connection_id and connections:
            connection_id = connections[0].connection_id
            log.debug(
                f"SYNC: websocket not directly mapped; falling back to first active connection={connection_id}"
            )

        if not connection_id:
            log.warning(
                f"SYNC requested for session {session.session_id} but no active connections found"
            )
            return

        # Fast-forward broker's view of iOS ACK state to reduce unnecessary replays
        try:
            if isinstance(last_received_seq, int):
                await self.ack_manager.ack_from_ios(session.tab_id, last_received_seq)
        except Exception:
            # Non-fatal: proceed with replay logic regardless
            pass

        # Delegate to session manager's replay logic (sends start/end sync_status + messages)
        log.info(
            f"Processing SYNC for tab={session.tab_id} session={session.session_id} on connection={connection_id}"
        )
        await self.session_manager._replay_messages(session.session_id, connection_id)
        log.info(
            f"SYNC completed (replay issued) for tab={session.tab_id} session={session.session_id}"
        )
