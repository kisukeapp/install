"""
Route handler for managing API routes.
"""
import logging
from websockets import WebSocketServerProtocol

from .base import BaseHandler
from ..models import ErrorCode

log = logging.getLogger(__name__)


class RouteHandler(BaseHandler):
    """Handles route management messages."""

    async def handle_routes(self, data: dict, ws: WebSocketServerProtocol):
        """Handle routes request."""
        new_routes = data.get('payload') or data.get('routesPayload')

        if new_routes:
            try:
                for route in new_routes:
                    config = route.get('config', route)
                    log.info(
                        "Registering route token=%s provider=%s model=%s base_url=%s auth=%s",
                        route.get('token'),
                        config.get('provider'),
                        config.get('model'),
                        config.get('base_url') or config.get('baseUrl'),
                        config.get('auth_method') or config.get('authMethod'),
                    )
                tokens = self.route_manager.register_routes(new_routes)
                log.info("Route registration successful tokens=%s", tokens)
                await self._send(ws, {
                    'type': 'routes_registered',
                    'tokens': tokens,
                    'routes': self.route_manager.get_all_routes(),
                    'activeRoute': self.route_manager.active_route_token,
                    'stableRoute': self.route_manager.stable_route_token
                })
            except Exception as route_error:
                log.error(f"Failed to register routes: {route_error}")
                await self._send_error(ws, f"Failed to register routes: {route_error}", None, ErrorCode.SYSTEM_ERROR)
        else:
            routes = self.route_manager.get_all_routes()
            await self._send(ws, {
                'type': 'routes',
                'routes': routes,
                'activeRoute': self.route_manager.active_route_token,
                'stableRoute': self.route_manager.stable_route_token
            })

    async def handle_set_active_route(self, data: dict, ws: WebSocketServerProtocol):
        """Handle set active route request."""
        token = data.get('token')
        if not token:
            await self._send_error(ws, "Missing route token", None, ErrorCode.INVALID_ROUTE_TOKEN)
            return

        try:
            success = await self.route_manager.set_active_route(token)
            log.info("Set active route token=%s success=%s", token, success)
            await self._send(ws, {
                'type': 'route_updated',
                'success': success,
                'activeRoute': token if success else self.route_manager.active_route_token
            })
        except Exception as e:
            log.error(f"Failed to set active route: {e}")
            await self._send_error(ws, f"Failed to set active route: {e}", None, ErrorCode.SYSTEM_ERROR)

    async def handle_set_stable_route(self, data: dict, ws: WebSocketServerProtocol):
        """Handle set stable route request."""
        token = data.get('token')
        if not token:
            await self._send_error(ws, "Missing route token", None, ErrorCode.INVALID_ROUTE_TOKEN)
            return

        try:
            success = await self.route_manager.set_stable_route(token)
            log.info("Set stable route token=%s success=%s", token, success)
            await self._send(ws, {
                'type': 'route_updated',
                'success': success,
                'stableRoute': token if success else self.route_manager.stable_route_token
            })
        except Exception as e:
            log.error(f"Failed to set stable route: {e}")
            await self._send_error(ws, f"Failed to set stable route: {e}", None, ErrorCode.SYSTEM_ERROR)
