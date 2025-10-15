"""
Message handler for send/edit operations.
"""
import asyncio
import logging
import time
from websockets import WebSocketServerProtocol

from .base import BaseHandler
from ..models import MessageType, SessionState, ErrorCode

log = logging.getLogger(__name__)


class MessageHandler(BaseHandler):
    """Handles message send/edit operations."""

    async def handle_send(self, data: dict, ws: WebSocketServerProtocol):
        """Handle message send request with route tracking. Extracts tabId from message."""
        try:
            # Extract session from tabId
            session = await self._get_session_from_message(data, ws)
            if not session:
                return

            content = data.get('content')
            if not content:
                log.error("No content in send request")
                await self._send_error(ws, "Missing message content", session.tab_id, ErrorCode.MISSING_CONTENT)
                return

            # Extract optional message UUID from iOS (for editing support)
            message_uuid = data.get('messageUuid')

            # Process iOS message with sequential ordering
            ios_seq = data.get('seq')  # iOS may provide its own seq
            # Use tab_id directly for ACK tracking (consistent across all message types)
            ready_messages = await self.ack_manager.process_ios_message(session.tab_id, ios_seq, message_data=None)

            # Send ACKs for all ready messages in order
            for broker_ack_seq, is_duplicate in ready_messages:
                ack_seq = await self.ack_manager.get_next_broker_seq(session.tab_id)
                await self._send(ws, {
                    'type': MessageType.MESSAGE_RECEIVED_ACK,
                    'tabId': session.tab_id,
                    'ack_seq': broker_ack_seq,
                    'seq': ack_seq,  # Broker's own sequence for this ACK
                    'is_duplicate': is_duplicate
                })
                log.debug(f"Sent ACK for SEND message seq={broker_ack_seq} (duplicate={is_duplicate})")

            # If no messages ready (buffered) or duplicate, don't process further
            if not ready_messages:
                log.info(f"SEND message seq={ios_seq} buffered - waiting for earlier messages")
                return

            if ready_messages[0][1]:  # First message is duplicate
                log.info(f"Ignoring duplicate message seq={ios_seq} from iOS")
                return

            # Check global credentials - request from iOS if missing
            if not self.broker.global_credentials:
                log.info("No global credentials available - requesting from iOS")
                from .credentials import CredentialsHandler
                cred_handler = CredentialsHandler(
                    self.broker, self.session_manager, self.connection_manager,
                    self.message_buffer, self.route_manager, self.claude_interface, self.ack_manager
                )
                await cred_handler.request_credentials_from_ios(ws)
                await self._send_error(
                    ws, "Credentials required - requesting from iOS",
                    session.tab_id, ErrorCode.NO_ACTIVE_ROUTE
                )
                return

            # Create message content (credentials are global)
            message_content = {
                'type': 'user_message',
                'content': content,
                'timestamp': time.time()
            }
            log.info(
                "Dispatching message session=%s tab=%s state=%s uuid=%s content_preview=%s",
                session.session_id,
                session.tab_id,
                session.state,
                message_uuid,
                str(content)[:160]
            )

            # Send to Claude if active
            if session.state == SessionState.ACTIVE and session.claude_session_id:
                try:
                    # Debug timing of message processing
                    log.info(f"[SEND DEBUG] Processing SEND message seq={ios_seq} for session {session.session_id}")
                    log.info(f"[SEND DEBUG] Current session permission_mode in broker: {session.permission_mode}")

                    # Define callback to stream response events to iOS
                    async def stream_response(event):
                        """Callback to forward Claude events to iOS"""
                        # Debug init events specifically
                        if isinstance(event, dict) and event.get('type') == 'system' and event.get('subtype') == 'init':
                            log.info(f"[INIT DEBUG] Claude CLI sent init event with permissionMode: {event.get('data', {}).get('permissionMode', 'unknown')}")
                            log.info(f"[INIT DEBUG] Expected mode (from broker session): {session.permission_mode}")

                        message = {
                            'type': 'claude_event',
                            'data': event,
                            'tabId': session.tab_id
                        }
                        # Always buffer messages - iOS may reconnect and needs to replay
                        await self.session_manager.send_message(session.session_id, message)

                    # Wrap streaming in error handler to catch silent failures
                    async def safe_stream_task():
                        """Safe wrapper for streaming task with error handling"""
                        try:
                            log.info(f"[SEND DEBUG] Forwarding message to Claude CLI now...")
                            await self.claude_interface.send_message(
                                session.claude_session_id,
                                content,
                                credentials=self.broker.global_credentials,
                                message_uuid=message_uuid,
                                response_callback=stream_response
                            )
                            log.info(f"Completed streaming for session {session.session_id}")
                        except Exception as stream_error:
                            log.error(
                                f"Streaming task failed for session {session.session_id}: {stream_error}",
                                exc_info=True
                            )
                            # Send error to iOS
                            try:
                                await self._send_error(
                                    ws, f"Streaming failed: {stream_error}",
                                    session.tab_id, ErrorCode.CLAUDE_SEND_FAILED
                                )
                            except Exception as error_send_error:
                                log.error(
                                    f"Failed to send streaming error to iOS: {error_send_error}"
                                )

                    # DON'T await - run in background so WebSocket loop can process permission_response
                    asyncio.create_task(safe_stream_task())
                    log.info(f"Started Claude message send task for session {session.session_id}")
                except Exception as e:
                    log.error(f"Failed to send to Claude: {e}")
                    await self._send_error(
                        ws, f"Failed to send to Claude: {e}",
                        session.tab_id, ErrorCode.CLAUDE_SEND_FAILED
                    )

                    # Try to buffer the message as fallback
                    try:
                        await self.session_manager.send_message(session.session_id, message_content)
                        log.info("Message buffered as fallback after Claude send failure")
                    except Exception as be:
                        log.error(f"Fallback buffering also failed: {be}")
                        await self._send_error(
                            ws, f"Complete send failure: {be}",
                            session.tab_id, ErrorCode.SYSTEM_ERROR
                        )
                    return
            else:
                # Session not active, buffer the message
                try:
                    await self.session_manager.send_message(session.session_id, message_content)
                    log.info(f"Buffered message for session {session.session_id} (state: {session.state})")
                except Exception as e:
                    log.error(f"Failed to buffer message: {e}")
                    await self._send_error(ws, f"Failed to buffer message: {e}", session.tab_id, ErrorCode.SYSTEM_ERROR)
                    return

        except Exception as general_error:
            log.error(f"Unexpected error in handle_send: {general_error}")
            tab_id = data.get('tabId') if data else None
            await self._send_error(ws, f"Internal error: {general_error}", tab_id, ErrorCode.SYSTEM_ERROR)

    async def handle_edit_message(self, data: dict, ws: WebSocketServerProtocol, connection_id: str):
        """
        Handle message edit request from iOS. Extracts tabId from message.
        This creates a branch by resuming at a specific message UUID.
        """
        try:
            # Extract session from tabId
            session = await self._get_session_from_message(data, ws)
            if not session:
                return

            message_uuid = data.get('messageUuid')
            new_content = data.get('newContent')

            if not message_uuid:
                await self._send_error(ws, "messageUuid required for edit", session.tab_id)
                return

            if not new_content:
                await self._send_error(ws, "newContent required for edit", session.tab_id)
                return

            # Check credentials
            if not self.broker.global_credentials:
                from .credentials import CredentialsHandler
                cred_handler = CredentialsHandler(
                    self.broker, self.session_manager, self.connection_manager,
                    self.message_buffer, self.route_manager, self.claude_interface, self.ack_manager
                )
                await cred_handler.request_credentials_from_ios(ws)
                await self._send_error(ws, "Credentials required for edit", session.tab_id, ErrorCode.NO_ACTIVE_ROUTE)
                return

            log.info(f"Processing message edit for session {session.session_id} at UUID {message_uuid}")

            # Close current Claude session if active
            if session.claude_session_id:
                try:
                    await self.claude_interface.close_session(session.claude_session_id)
                    log.info(f"Closed Claude session {session.claude_session_id} for branching")
                except Exception as e:
                    log.warning(f"Error closing Claude session for edit: {e}")

            # Store branch information
            async with self.session_manager._lock:
                session.branch_point_uuid = message_uuid
                if not session.original_session_id:
                    session.original_session_id = session.session_id

            # Create new Claude session with resume parameters
            try:
                # Determine the original session to resume from
                resume_session_id = session.original_session_id or session.session_id

                # Create session with resume
                # (SessionManager already registered proxy route during create_session)
                claude_session = await self.claude_interface.create_session_with_resume(
                    credentials=self.broker.global_credentials,
                    session_id=session.session_id,
                    tab_id=session.tab_id,  # For iOS permission routing
                    workdir=session.workdir,
                    system_prompt=session.system_prompt,
                    resume_session_id=resume_session_id,
                    resume_at_message_uuid=message_uuid
                )

                # Update session with new Claude ID
                async with self.session_manager._lock:
                    session.claude_session_id = claude_session.session_id
                    session.state = SessionState.ACTIVE

                # Note: No longer need persistent listener - responses are streamed per-query
                # asyncio.create_task(self._listen_to_claude(session.session_id, claude_session))

                # Send edit acknowledgement
                await self._send(ws, {
                    'type': 'edit_acknowledged',
                    'tabId': session.tab_id,
                    'branchPoint': message_uuid
                })

                # Define callback to stream response events to iOS
                async def stream_response(event):
                    """Callback to forward Claude events to iOS"""
                    message = {
                        'type': 'claude_event',
                        'data': event,
                        'tabId': session.tab_id
                    }
                    # Always buffer messages - iOS may reconnect and needs to replay
                    await self.session_manager.send_message(session.session_id, message)

                # Wrap streaming in error handler to catch silent failures
                async def safe_stream_task():
                    """Safe wrapper for streaming task with error handling"""
                    try:
                        await self.claude_interface.send_message(
                            claude_session.session_id,
                            new_content,
                            credentials=self.broker.global_credentials,
                            response_callback=stream_response
                        )
                        log.info(f"Completed streaming for edited message in session {session.session_id}")
                    except Exception as stream_error:
                        log.error(
                            f"Edit streaming task failed for session {session.session_id}: {stream_error}",
                            exc_info=True
                        )
                        # Send error to iOS
                        try:
                            await self._send_error(
                                ws, f"Edit streaming failed: {stream_error}",
                                session.tab_id, ErrorCode.CLAUDE_SEND_FAILED
                            )
                        except Exception as error_send_error:
                            log.error(
                                f"Failed to send edit streaming error to iOS: {error_send_error}"
                            )

                # Now send the new message content with response streaming
                # DON'T await - run in background so WebSocket loop can process permission_response
                asyncio.create_task(safe_stream_task())
                log.info(f"Started Claude message send task for edited message in session {session.session_id}")

            except Exception as e:
                log.error(f"Failed to create branched Claude session: {e}")
                async with self.session_manager._lock:
                    session.state = SessionState.ERROR
                await self._send_error(ws, f"Failed to branch session: {e}", session.tab_id)

        except Exception as e:
            log.error(f"Error handling edit message: {e}")
            tab_id = data.get('tabId') if data else None
            await self._send_error(ws, f"Edit message failed: {e}", tab_id)

    async def handle_interrupt(self, data: dict, ws: WebSocketServerProtocol):
        """Handle interrupt request from iOS. Extracts tabId from message."""
        # Extract session from tabId
        session = await self._get_session_from_message(data, ws)
        if not session:
            return

        # Get Claude session and call interrupt
        claude_session = self.claude_interface.get_session(session.session_id)
        if claude_session and claude_session.client:
            try:
                await claude_session.client.interrupt()
                log.info(f"Sent interrupt to Claude session {session.session_id}")

                # Acknowledge to iOS with sequence
                ack_seq = await self.ack_manager.get_next_broker_seq(session.session_id)
                await self._send(ws, {
                    'type': 'interrupt_acknowledged',
                    'tabId': session.tab_id,
                    'status': 'success',
                    'seq': ack_seq
                })
            except Exception as e:
                log.error(f"Failed to interrupt session {session.session_id}: {e}")
                await self._send_error(ws, f"Interrupt failed: {e}", session.tab_id, ErrorCode.SYSTEM_ERROR)
        else:
            log.warning(f"No active Claude client for session {session.session_id}")
            await self._send_error(
                ws, "No active Claude session to interrupt",
                session.tab_id, ErrorCode.SESSION_NOT_FOUND
            )
