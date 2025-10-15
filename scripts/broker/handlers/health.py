"""
Health handler for health checks and status.
"""
import logging
import time
from websockets import WebSocketServerProtocol

from .base import BaseHandler

log = logging.getLogger(__name__)


class HealthHandler(BaseHandler):
    """Handles health check and status messages."""

    async def handle_health(self, data: dict, ws: WebSocketServerProtocol):
        """Handle health check request."""
        await self._send(ws, {
            'type': 'health',
            'status': 'ok',
            'broker_running': True,
            'has_credentials': self.broker.global_credentials is not None
        })

    async def handle_status(self, ws: WebSocketServerProtocol):
        """Handle status request."""
        stats = self.session_manager.get_stats()
        sessions = self.session_manager.get_all_sessions()

        await self._send(ws, {
            'type': 'status',
            'stats': stats,
            'sessions': sessions
        })
