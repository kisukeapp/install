"""
Credentials handler for managing API credentials.
"""
import logging
import time
from websockets import WebSocketServerProtocol

from .base import BaseHandler
from ..models import MessageType, ClaudeApiCredentials
from ..utils import mask_secret

log = logging.getLogger(__name__)


class CredentialsHandler(BaseHandler):
    """Handles credential-related messages."""

    async def handle_update_credentials(self, data: dict, ws: WebSocketServerProtocol):
        """
        Handle credential update from iOS.
        iOS can update credentials at any point.
        """
        tab_id = data.get('tabId')

        # Process iOS message with sequential ordering
        ios_seq = data.get('seq')
        # Use tab_id directly for ACK tracking (consistent across all message types)
        session_id_for_ack = tab_id or 'global'
        ready_messages = await self.ack_manager.process_ios_message(session_id_for_ack, ios_seq, message_data=None)

        # Send ACKs for all ready messages in order
        if tab_id:
            for broker_ack_seq, is_duplicate in ready_messages:
                ack_seq = await self.ack_manager.get_next_broker_seq(tab_id)
                await self._send(ws, {
                    'type': MessageType.MESSAGE_RECEIVED_ACK,
                    'tabId': tab_id,
                    'ack_seq': broker_ack_seq,
                    'seq': ack_seq,
                    'is_duplicate': is_duplicate
                })
                log.debug(f"Sent ACK for UPDATE_CREDENTIALS seq={broker_ack_seq}")

            if not ready_messages:
                log.info(f"UPDATE_CREDENTIALS seq={ios_seq} buffered - waiting for earlier messages")

        claude_config = data.get('claudeConfig')
        if not claude_config:
            await self._send_error(ws, "claudeConfig required in update_credentials")
            return

        log.info(
            "Updating credentials provider=%s model=%s base_url=%s auth=%s key=%s",
            claude_config.get('provider'),
            claude_config.get('model'),
            claude_config.get('baseUrl'),
            claude_config.get('authMethod'),
            mask_secret(claude_config.get('apiKey')),
        )

        # Update global credentials
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
            await self._send_error(ws, "API key required in claudeConfig")
            return

        # Send confirmation with sequence
        if tab_id:
            confirm_seq = await self.ack_manager.get_next_broker_seq(tab_id)
            await self._send(ws, {
                'type': 'credentials_updated',
                'status': 'success',
                'tabId': tab_id,
                'seq': confirm_seq
            })
        else:
            await self._send(ws, {
                'type': 'credentials_updated',
                'status': 'success'
            })
        log.info("Global credentials updated from iOS")

        # Update all active session routes with new credentials (queued for next turn)
        self._update_all_session_credentials(self.broker.global_credentials)

        # Ensure proxy bridge token reflects the latest credentials
        self.route_manager.sync_bridge_route()

    async def request_credentials_from_ios(self, ws: WebSocketServerProtocol):
        """
        Request credentials from iOS when broker doesn't have them.
        """
        await self._send(ws, {
            'type': MessageType.REQUEST_CREDENTIALS,
            'reason': 'Broker requires credentials to process messages'
        })
        log.info("Requesting credentials from iOS")

    def _update_all_session_credentials(self, credentials):
        """
        Update credentials for all active sessions (queued for next turn).

        Credentials will be applied on the next inbound request for each session.

        Args:
            credentials: ClaudeApiCredentials to update
        """
        from proxy.registry import update_credentials
        from proxy.config import ModelConfig

        # Get all active sessions
        all_sessions = list(self.session_manager._sessions.values())

        config = ModelConfig(
            provider=credentials.provider,
            base_url=credentials.base_url,
            api_key=credentials.api_key,
            model=credentials.model,
            auth_method=credentials.auth_method,
            extra_headers=credentials.extra_headers or {},
            azure_deployment=credentials.azure_deployment,
            azure_api_version=credentials.azure_api_version
        )

        for session in all_sessions:
            if session.claude_session_id:  # Only update active Claude sessions
                session_token = f"kisuke-{session.session_id[:5]}"
                update_credentials(session_token, config)
                log.debug(f"Queued credential update for session {session.session_id} (will apply on next turn)")

        log.info(f"Queued credential updates for {len(all_sessions)} active sessions")
