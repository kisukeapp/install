"""
WebSocket message handlers for Kisuke Broker - Refactored with SoC.
"""
import asyncio
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
from ..models import MessageType

from .utils import generate_short_id
from .credentials import CredentialsHandler
from .session import SessionHandler
from .message import MessageHandler
from .routes import RouteHandler
from .permissions import PermissionHandler
from .health import HealthHandler
from .ack import AckHandler
from .conversations import ConversationHandler

log = logging.getLogger(__name__)


class MessageHandlers:
    """Main orchestrator that delegates to specialized handlers."""

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
        Initialize message handlers orchestrator.

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

        # Initialize specialized handlers
        self.credentials_handler = CredentialsHandler(
            broker, session_manager, connection_manager, message_buffer,
            route_manager, claude_interface, self.ack_manager
        )
        self.session_handler = SessionHandler(
            broker, session_manager, connection_manager, message_buffer,
            route_manager, claude_interface, self.ack_manager
        )
        self.message_handler = MessageHandler(
            broker, session_manager, connection_manager, message_buffer,
            route_manager, claude_interface, self.ack_manager
        )
        self.route_handler = RouteHandler(
            broker, session_manager, connection_manager, message_buffer,
            route_manager, claude_interface, self.ack_manager
        )
        self.permission_handler = PermissionHandler(
            broker, session_manager, connection_manager, message_buffer,
            route_manager, claude_interface, self.ack_manager
        )
        self.health_handler = HealthHandler(
            broker, session_manager, connection_manager, message_buffer,
            route_manager, claude_interface, self.ack_manager
        )
        self.ack_handler = AckHandler(
            broker, session_manager, connection_manager, message_buffer,
            route_manager, claude_interface, self.ack_manager
        )
        self.conversation_handler = ConversationHandler(
            broker, session_manager, connection_manager, message_buffer,
            route_manager, claude_interface, self.ack_manager
        )

    async def handle_connection(self, ws: WebSocketServerProtocol, path: str):
        """
        Handle a new WebSocket connection.
        Single WebSocket can handle multiple tabs - each message includes tabId.

        Args:
            ws: WebSocket connection
            path: Request path
        """
        connection_id = f"conn_{generate_short_id()}"

        try:
            # Add connection to manager
            await self.connection_manager.add_connection(connection_id, ws)
            log.info(f"New connection {connection_id} from {ws.remote_address}")

            # Send initial connected event to iOS with new protocol format
            # Use a dummy session for initial connection
            init_seq = await self.ack_manager.get_next_broker_seq(f"conn_{connection_id}")
            await self._send(ws, {
                'type': 'system',
                'status': 'connected',
                'connection_id': connection_id,
                'seq': init_seq
            })
            log.info(f"Sent connected event to {connection_id} with seq={init_seq}")

            # Process messages - each message includes tabId for routing
            async for message in ws:
                log.debug(f"Raw message received on {connection_id}: {message[:200]}...")
                try:
                    data = json.loads(message)
                    msg_type = data.get('type')

                    log.info(f"Received message type: {msg_type} from {connection_id}")

                    # Route to appropriate handler
                    if msg_type == MessageType.START:
                        await self.session_handler.handle_start(data, ws, connection_id)
                    elif msg_type == MessageType.SEND:
                        await self.message_handler.handle_send(data, ws)
                    elif msg_type == MessageType.UPDATE_CREDENTIALS:
                        await self.credentials_handler.handle_update_credentials(data, ws)
                    elif msg_type == MessageType.ROUTES:
                        await self.route_handler.handle_routes(data, ws)
                    elif msg_type == MessageType.SET_ACTIVE_ROUTE:
                        await self.route_handler.handle_set_active_route(data, ws)
                    elif msg_type == MessageType.SET_STABLE_ROUTE:
                        await self.route_handler.handle_set_stable_route(data, ws)
                    elif msg_type == MessageType.HEALTH:
                        await self.health_handler.handle_health(data, ws)
                    elif msg_type == MessageType.PERMISSION_RESPONSE:
                        await self.permission_handler.handle_permission_response(data, ws)
                    elif msg_type == MessageType.STATUS:
                        await self.health_handler.handle_status(ws)
                    elif msg_type == MessageType.RESPONSE_ACK:
                        await self.ack_handler.handle_response_ack(data, ws)
                    elif msg_type == MessageType.EDIT_MESSAGE:
                        await self.message_handler.handle_edit_message(data, ws, connection_id)
                    elif msg_type == MessageType.INTERRUPT:
                        await self.message_handler.handle_interrupt(data, ws)
                    elif msg_type == MessageType.SET_PERMISSION_MODE:
                        await self.permission_handler.handle_set_permission_mode(data, ws)
                    elif msg_type == MessageType.REQUEST_CONVERSATIONS:
                        await self.conversation_handler.handle_request_conversations(data, ws)
                    elif msg_type == MessageType.LOAD_CONVERSATION:
                        await self.conversation_handler.handle_load_conversation(data, ws, connection_id)
                    elif msg_type == MessageType.SHUTDOWN:
                        await self.session_handler.handle_shutdown(data, connection_id)
                        break
                    else:
                        log.warning(f"Unknown message type: {msg_type}")

                except json.JSONDecodeError:
                    log.error("Invalid JSON received")
                    await self._send_error(ws, "Invalid JSON")
                except Exception as e:
                    log.error(f"Error handling message type {data.get('type') if 'data' in locals() else 'unknown'}: {e}", exc_info=True)
                    await self._send_error(ws, str(e))

        except Exception as e:
            log.error(f"Connection error: {e}")
        finally:
            # Cleanup - detach all sessions on this connection
            if connection_id:
                await self.session_handler.detach_all_sessions_for_connection(connection_id)
                await self.connection_manager.remove_connection(connection_id)
            log.info(f"Connection {connection_id} closed")

    async def send_permission_request_to_ios(self, tool_name: str, tool_input: dict, request_id: str):
        """
        Delegate to permission handler.

        Args:
            tool_name: Name of the tool requesting permission
            tool_input: Tool input parameters
            request_id: Unique request identifier
        """
        await self.permission_handler.send_permission_request_to_ios(tool_name, tool_input, request_id)

    async def _send(self, ws: WebSocketServerProtocol, data: dict):
        """Send data to WebSocket."""
        try:
            await ws.send(json.dumps(data))
        except Exception as e:
            log.error(f"Failed to send to WebSocket: {e}")

    async def _send_error(self, ws: WebSocketServerProtocol, error: str, tab_id: str = None, error_code: str = None):
        """Send error message to WebSocket."""
        error_msg = {
            'type': 'error',
            'error': error,
            'tabId': tab_id
        }
        if error_code:
            error_msg['errorCode'] = error_code

        await self._send(ws, error_msg)


__all__ = ['MessageHandlers']
