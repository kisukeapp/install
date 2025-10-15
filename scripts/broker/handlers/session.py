"""
Session handler for managing session lifecycle.
"""
import logging
import time
from websockets import WebSocketServerProtocol

from .base import BaseHandler
from .utils import generate_short_id
from ..models import MessageType, SessionState, ClaudeApiCredentials
from ..utils import mask_secret

log = logging.getLogger(__name__)


class SessionHandler(BaseHandler):
    """Handles session lifecycle messages."""

    async def handle_start(self, data: dict, ws: WebSocketServerProtocol, connection_id: str):
        """
        Handle session start request with iOS-provided API credentials.
        iOS only knows tabId - broker manages tabId→sessionId mapping.
        """
        tab_id = data.get('tabId')
        if not tab_id:
            await self._send_error(ws, "tabId required")
            return

        # Process iOS message with sequential ordering
        ios_seq = data.get('seq')  # iOS may provide its own seq
        # Use tab_id directly for ACK tracking (consistent across all message types)
        ready_messages = await self.ack_manager.process_ios_message(tab_id, ios_seq, message_data=None)

        # Send ACKs for all ready messages in order
        for broker_ack_seq, is_duplicate in ready_messages:
            ack_seq = await self.ack_manager.get_next_broker_seq(tab_id)
            await self._send(ws, {
                'type': MessageType.MESSAGE_RECEIVED_ACK,
                'tabId': tab_id,
                'ack_seq': broker_ack_seq,
                'seq': ack_seq,  # Broker's own sequence for this ACK
                'is_duplicate': is_duplicate
            })
            log.debug(f"Sent ACK for iOS START message seq={broker_ack_seq}")

        if not ready_messages:
            log.info(f"iOS START message seq={ios_seq} buffered - waiting for earlier messages")

        # Extract parameters from START message
        workdir = data.get('workdir', '/tmp')
        system_prompt = data.get('systemPrompt')
        permission_mode = data.get('permissionMode', 'prompt')

        # Process credentials from iOS - ALWAYS update for both new and existing sessions
        # This ensures proxy routes are current before Claude CLI makes any requests
        claude_config = data.get('claudeConfig')
        if claude_config:
            # iOS provided credentials - store them globally
            log.info(
                "START received credentials: tab_id=%s provider=%s model=%s base_url=%s auth=%s key=%s",
                tab_id,
                claude_config.get('provider'),
                claude_config.get('model'),
                claude_config.get('baseUrl'),
                claude_config.get('authMethod'),
                mask_secret(claude_config.get('apiKey')),
            )
            self.broker.global_credentials = ClaudeApiCredentials(
                credential_id=f"global_{int(time.time())}",
                provider=claude_config.get('provider', 'anthropic'),
                model=claude_config.get('model', 'claude-3-sonnet-20240229'),
                base_url=claude_config.get('baseUrl', 'https://api.anthropic.com'),
                api_key=claude_config.get('apiKey'),
                auth_method=claude_config.get('authMethod'),
                extra_headers=claude_config.get('extraHeaders', {}),
                azure_deployment=claude_config.get('azureDeployment'),
                azure_api_version=claude_config.get('azureApiVersion')
            )

            # Sync credentials to SessionManager for route registration
            self.session_manager.global_credentials = self.broker.global_credentials

            if not self.broker.global_credentials.api_key:
                await self._send_error(ws, "API key required in claudeConfig", tab_id)
                return
        elif not self.broker.global_credentials:
            # No credentials stored and none provided - request from iOS
            from .credentials import CredentialsHandler
            cred_handler = CredentialsHandler(
                self.broker, self.session_manager, self.connection_manager,
                self.message_buffer, self.route_manager, self.claude_interface, self.ack_manager
            )
            await cred_handler.request_credentials_from_ios(ws)
            await self._send_error(ws, "Credentials required - requesting from iOS", tab_id)
            return

        # Get or create session for tab
        session = await self.session_manager.get_session_by_tab(tab_id)

        if not session:
            # Create new session
            log.debug(
                "Creating session tab_id=%s workdir=%s permission=%s",
                tab_id,
                workdir,
                permission_mode,
            )

            session = await self.session_manager.create_session(
                tab_id=tab_id,
                initial_connection_id=connection_id,
                workdir=workdir,
                system_prompt=system_prompt,
                permission_mode=permission_mode
            )
        else:
            # Existing session - handle reconnection with ACK state
            last_received_seq = data.get('last_received_seq', -1)

            # Reset iOS→Broker sequence tracking since iOS is starting fresh
            # iOS will send messages starting from seq=1
            await self.ack_manager.reset_ios_tracking(session.session_id)

            # Get reconnection info
            reconnect_info = await self.ack_manager.get_ios_reconnect_info(
                session.session_id,
                last_received_seq
            )

            log.info(
                "iOS reconnecting to session %s, last_received_seq=%d, missed_count=%d",
                session.session_id,
                last_received_seq,
                reconnect_info['missed_count']
            )

            # Re-register proxy route with updated credentials
            # This ensures Claude CLI requests use fresh credentials from iOS
            self.session_manager._register_session_route(session.session_id)
            log.info(f"Re-registered proxy route for session {session.session_id} with updated credentials")

            # Attach connection to existing session (this triggers replay)
            # Note: _replay_messages uses ack_manager state (persistent), not client_info (ephemeral)
            # Note: _replay_messages will send sync_status at start and end of replay
            await self.session_manager.attach_connection(session.session_id, connection_id)

        # Start Claude session if needed
        if not session.claude_session_id:
            try:
                # Create Claude session using global credentials
                # (SessionManager already registered proxy route during create_session)
                claude_session = await self.claude_interface.create_session(
                    credentials=self.broker.global_credentials,
                    session_id=session.session_id,
                    tab_id=session.tab_id,  # For iOS permission routing
                    workdir=workdir,
                    system_prompt=system_prompt
                )

                # Update session with Claude ID
                async with self.session_manager._lock:
                    session.claude_session_id = claude_session.session_id
                    session.state = SessionState.ACTIVE

                # Set initial permission mode if specified
                if session.permission_mode and session.permission_mode != 'prompt':
                    # Valid modes for Claude CLI: default, acceptEdits, plan, bypassPermissions
                    # Map 'prompt' to 'default' if needed
                    mode_to_set = 'default' if session.permission_mode == 'prompt' else session.permission_mode

                    if claude_session.client:
                        try:
                            await claude_session.client.set_permission_mode(mode_to_set)
                            log.info(f"Set initial permission mode to '{mode_to_set}' for session {session.session_id}")
                        except Exception as e:
                            log.warning(f"Failed to set initial permission mode: {e}")

                # Note: No longer need persistent listener - responses are streamed per-query
                # asyncio.create_task(self._listen_to_claude(session.session_id, claude_session))

                # Send ready status with sequence number
                status_seq = await self.ack_manager.get_next_broker_seq(tab_id)
                await self._send(ws, {
                    'type': 'status',
                    'status': 'ready',
                    'tabId': session.tab_id,
                    'seq': status_seq
                })

            except Exception as e:
                log.error(f"Failed to start Claude session: {e}")
                async with self.session_manager._lock:
                    session.state = SessionState.ERROR
                await self._send_error(ws, f"Failed to start Claude: {e}", session.tab_id)
                return
        else:
            # Session already has Claude, just send ready with sequence
            status_seq = await self.ack_manager.get_next_broker_seq(tab_id)
            await self._send(ws, {
                'type': 'status',
                'status': 'ready',
                'tabId': session.tab_id,
                'resumed': True,
                'seq': status_seq
            })

    async def handle_shutdown(self, data: dict, connection_id: str):
        """Handle shutdown request. Closes WebSocket connection."""
        # No session needed - shutdown closes the entire connection
        # All sessions on this connection will be detached in finally block
        log.info(f"Shutdown requested for connection {connection_id}")

    async def detach_all_sessions_for_connection(self, connection_id: str):
        """
        Detach all sessions attached to a connection.

        Args:
            connection_id: Connection identifier
        """
        # Get all sessions that have this connection
        all_session_ids = list(self.connection_manager._session_connections.keys())

        for session_id in all_session_ids:
            connections = await self.connection_manager.get_session_connections(session_id)
            if any(c.connection_id == connection_id for c in connections):
                await self.session_manager.detach_connection(connection_id)
                log.debug(f"Detached session {session_id} from connection {connection_id}")
