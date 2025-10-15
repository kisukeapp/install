"""
Conversation handler for managing conversation requests.
"""
import logging
import time
from websockets import WebSocketServerProtocol

from .base import BaseHandler
from ..utils import list_conversations_for_project, load_conversation_events, mask_secret
from ..models import SessionState, ClaudeApiCredentials

log = logging.getLogger(__name__)


class ConversationHandler(BaseHandler):
    """Handles conversation-related messages."""

    async def handle_request_conversations(self, data: dict, ws: WebSocketServerProtocol):
        """Handle request for conversations from iOS."""
        log.info("Received REQUEST_CONVERSATIONS from iOS")

        cwd = data.get('cwd')
        if not cwd:
            log.error("REQUEST_CONVERSATIONS missing cwd")
            await self._send_error(ws, "cwd required for requesting conversations")
            return

        log.info(f"Listing conversations for project: {cwd}")

        # Get conversations for this project only
        conversations = list_conversations_for_project(cwd)

        log.info(f"Found {len(conversations)} conversations for project: {cwd}")

        # Send response
        await self._send(ws, {
            'type': 'conversations',
            'cwd': cwd,
            'conversations': conversations
        })
        log.info(f"Sent conversations list to iOS: {len(conversations)} items")

    async def handle_load_conversation(self, data: dict, ws: WebSocketServerProtocol, connection_id: str):
        """
        Load and resume a conversation from history.

        Flow:
        1. iOS sends load_conversation with {tabId, sessionId (from .jsonl), cwd, claudeConfig}
        2. Broker creates/gets session for tabId
        3. Broker starts Claude CLI with resume=sessionId
        4. Once Claude starts, broker loads .jsonl events
        5. Broker sends events as batch to iOS
        6. iOS renders conversation

        Args:
            data: Request data with cwd, sessionId (conversation file), tabId, claudeConfig
            ws: WebSocket connection
            connection_id: Connection identifier
        """
        cwd = data.get('cwd')
        conversation_session_id = data.get('sessionId')  # This is the .jsonl filename
        tab_id = data.get('tabId')

        log.info(f"Received LOAD_CONVERSATION from iOS: tabId={tab_id}, sessionId={conversation_session_id}, cwd={cwd}")

        if not cwd:
            log.error(f"LOAD_CONVERSATION missing cwd for tab {tab_id}")
            await self._send_error(ws, "cwd required for loading conversation", tab_id)
            return

        if not conversation_session_id:
            log.error(f"LOAD_CONVERSATION missing sessionId for tab {tab_id}")
            await self._send_error(ws, "sessionId required for loading conversation", tab_id)
            return

        if not tab_id:
            log.error("LOAD_CONVERSATION missing tabId")
            await self._send_error(ws, "tabId required for loading conversation", tab_id)
            return

        # Extract and store credentials from iOS if provided
        claude_config = data.get('claudeConfig')
        if claude_config:
            log.info(
                "LOAD_CONVERSATION with credentials: tab_id=%s provider=%s model=%s base_url=%s auth=%s key=%s",
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

        # Process iOS message with sequential ordering
        ios_seq = data.get('seq')
        # Use tab_id directly for ACK tracking (consistent across all message types)
        ready_messages = await self.ack_manager.process_ios_message(tab_id, ios_seq, message_data=None)

        # Send ACKs for all ready messages in order
        for broker_ack_seq, is_duplicate in ready_messages:
            log.debug(f"Sending ACK for LOAD_CONVERSATION: tab={tab_id}, ios_seq={ios_seq}, broker_ack_seq={broker_ack_seq}")
            ack_seq = await self.ack_manager.get_next_broker_seq(tab_id)
            await self._send(ws, {
                'type': 'message_received_ack',
                'tabId': tab_id,
                'ack_seq': broker_ack_seq,
                'seq': ack_seq,
            'is_duplicate': is_duplicate
        })

        if not ready_messages:
            log.info(f"LOAD_CONVERSATION seq={ios_seq} buffered - waiting for earlier messages")
            return

        # Get or create broker session for this tab
        session = await self.session_manager.get_session_by_tab(tab_id)

        if not session:
            log.info(f"Creating new broker session for tab {tab_id}")
            # Create new broker session
            session = await self.session_manager.create_session(
                tab_id=tab_id,
                initial_connection_id=connection_id,
                workdir=cwd,
                system_prompt=None,
                permission_mode='prompt'
            )
            log.info(f"Created broker session {session.session_id} for tab {tab_id}")
        else:
            log.info(f"Using existing broker session {session.session_id} for tab {tab_id}")

            # If there's an existing Claude session, close it first to prevent stale process
            if session.claude_session_id:
                log.info(f"Closing existing Claude session {session.claude_session_id} before resuming with fresh credentials")
                await self.claude_interface.close_session(session.claude_session_id)
                async with self.session_manager._lock:
                    session.claude_session_id = None

        # Start Claude with resume
        if not session.claude_session_id:
            if not self.broker.global_credentials:
                log.error(f"No credentials available for loading conversation on tab {tab_id}")
                await self._send_error(ws, "Credentials required to load conversation", tab_id)
                return

            try:
                log.info(f"Starting Claude CLI with resume: broker_session={session.session_id}, resume_from={conversation_session_id}")

                # Create Claude session with resume
                claude_session = await self.claude_interface.create_session(
                    credentials=self.broker.global_credentials,
                    session_id=session.session_id,
                    tab_id=tab_id,
                    workdir=cwd,
                    system_prompt=None,
                    resume_session_id=conversation_session_id  # Resume from .jsonl session
                )

                # Update session with Claude ID
                async with self.session_manager._lock:
                    session.claude_session_id = claude_session.session_id
                    session.state = SessionState.ACTIVE

                log.info(f"Claude CLI started successfully: claude_session={claude_session.session_id}, resumed_from={conversation_session_id}")

            except Exception as e:
                log.error(f"Failed to resume Claude session {conversation_session_id}: {e}")
                await self._send_error(ws, f"Failed to resume conversation: {e}", tab_id)
                return

        # Load conversation events from .jsonl using ripgrep
        log.info(f"Loading conversation events from .jsonl: session={conversation_session_id}, cwd={cwd}")
        events = load_conversation_events(cwd, conversation_session_id, from_second_to_last_user=True)

        if not events:
            log.warning(f"No events found for conversation {conversation_session_id} in {cwd}")
            await self._send_error(ws, f"No events found for conversation {conversation_session_id}", tab_id)
            return

        log.info(f"Loaded {len(events)} events from conversation {conversation_session_id}")

        # Send all events as a batch message
        log.info(f"Sending conversation batch to iOS: tab={tab_id}, events_count={len(events)}")
        await self.session_manager.send_message_batch(
            session.session_id,
            events,
            message_type='conversation_events_batch',
            tab_id=tab_id
        )

        # Send ready status
        await self.session_manager.send_message(session.session_id, {
            'type': 'conversation_loaded',
            'tabId': tab_id,
            'sessionId': conversation_session_id,
            'eventCount': len(events)
        })

        log.info(f"Successfully loaded conversation {conversation_session_id} for tab {tab_id}: {len(events)} events sent as batch")
