"""
Core session manager that orchestrates sessions, connections, and messages.
Handles the lifecycle of Claude sessions and coordinates all components.
"""
import asyncio
import logging
import time
import json
from typing import Dict, Optional, List, Tuple, Any, Set
from dataclasses import dataclass, field
from enum import Enum

from ..models import SessionInfo, SessionState, Message
from ..utils import websocket_is_open
from .connection_manager import ConnectionManager
from .message_buffer import MessageBuffer

log = logging.getLogger(__name__)


class SessionManager:
    """
    Central orchestrator for session management.
    
    Coordinates:
    - Session lifecycle (create, destroy, persist)
    - Connection management (attach/detach WebSockets)
    - Message flow (buffering, delivery, acknowledgments)
    - Claude process management
    """
    
    def __init__(self,
                 connection_manager: ConnectionManager,
                 message_buffer: MessageBuffer,
                 session_timeout: int = 0,  # 0 = never timeout (persistent)
                 cleanup_interval: int = 60,
                 global_credentials: Optional[Any] = None):
        """
        Initialize session manager.

        Args:
            connection_manager: Connection manager instance
            message_buffer: Message buffer instance
            session_timeout: Session timeout in seconds (0 = persistent)
            cleanup_interval: Cleanup check interval in seconds
            global_credentials: Global credentials for proxy routes
        """
        self.connection_manager = connection_manager
        self.message_buffer = message_buffer
        self.session_timeout = session_timeout
        self.cleanup_interval = cleanup_interval
        self.global_credentials = global_credentials
        
        # Active sessions
        self._sessions: Dict[str, SessionInfo] = {}
        
        # Tab ID to session ID mapping
        self._tab_sessions: Dict[str, str] = {}
        
        # Claude process mapping (will be replaced with actual Claude SDK)
        self._claude_processes: Dict[str, Any] = {}
        
        # Lock for thread safety
        self._lock = asyncio.Lock()
        
        # Background tasks
        self._cleanup_task: Optional[asyncio.Task] = None
        self._message_processor_task: Optional[asyncio.Task] = None
    
    async def start(self):
        """Start the session manager and background tasks."""
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        self._message_processor_task = asyncio.create_task(self._process_messages_loop())
        log.info("Session manager started")
    
    async def stop(self):
        """Stop the session manager and cleanup resources."""
        # Cancel background tasks
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
        
        if self._message_processor_task:
            self._message_processor_task.cancel()
            try:
                await self._message_processor_task
            except asyncio.CancelledError:
                pass
        
        # Clean up all sessions
        session_ids = list(self._sessions.keys())
        for session_id in session_ids:
            await self.destroy_session(session_id)
        
        log.info("Session manager stopped")
    
    # === Session Lifecycle ===
    
    async def create_session(self,
                            tab_id: str,
                            initial_connection_id: Optional[str] = None,
                            workdir: str = "/tmp",
                            system_prompt: Optional[str] = None,
                            permission_mode: str = "prompt") -> SessionInfo:
        """
        Create a new session for an iOS tab.
        Credentials are stored globally in broker, not per session.
        
        Args:
            tab_id: iOS tab identifier
            initial_connection_id: Optional initial WebSocket connection
            workdir: Working directory
            system_prompt: Optional system prompt
            permission_mode: Permission mode
            
        Returns:
            Created SessionInfo
        """
        async with self._lock:
            # Check if tab already has a session
            if tab_id in self._tab_sessions:
                session_id = self._tab_sessions[tab_id]
                session = self._sessions.get(session_id)
                if session:
                    log.info(f"Tab {tab_id} already has session {session_id}")
                    # Attach new connection if provided
                    if initial_connection_id:
                        await self.attach_connection(session_id, initial_connection_id)
                    return session
            
            # Generate new session ID
            import uuid
            session_id = f"session_{uuid.uuid4().hex[:8]}"
            
            # Create session info (credentials are global)
            session = SessionInfo(
                session_id=session_id,
                tab_id=tab_id,
                state=SessionState.INITIALIZING,
                workdir=workdir,
                system_prompt=system_prompt,
                permission_mode=permission_mode
            )
            
            # Store session
            self._sessions[session_id] = session
            self._tab_sessions[tab_id] = session_id

            log.info(f"Created session {session_id} for tab {tab_id}")

        # Register proxy route BEFORE initializing Claude process
        # (Claude initialization makes API calls that need the route)
        self._register_session_route(session_id)
        
        # Initialize Claude process (placeholder - will integrate with real SDK)
        await self._initialize_claude_process(session_id)
        
        # Attach initial connection if provided
        if initial_connection_id:
            await self.attach_connection(session_id, initial_connection_id)
            # attach_connection sets state to ACTIVE
        else:
            # Mark session as ready (no connection yet)
            async with self._lock:
                session.state = SessionState.READY
        
        return session
    
    async def destroy_session(self, session_id: str, explicit: bool = True):
        """
        Destroy a session.

        Args:
            session_id: Session identifier
            explicit: Whether this is an explicit destruction (vs timeout)
        """
        async with self._lock:
            session = self._sessions.pop(session_id, None)
            if not session:
                return

            # Remove tab mapping
            if session.tab_id in self._tab_sessions:
                del self._tab_sessions[session.tab_id]

            # Mark as terminated
            session.state = SessionState.TERMINATED

        # Unregister proxy route
        self._unregister_session_route(session_id)
        
        # Get all connection IDs for this session (before removing)
        connection_ids = []
        connections = await self.connection_manager.get_session_connections(session_id)
        for conn_info in connections:
            connection_ids.append(conn_info.connection_id)
            # Ensure WebSocket is closed
            if conn_info.websocket and websocket_is_open(conn_info.websocket):
                try:
                    await conn_info.websocket.close()
                except:
                    pass
        
        # Remove all connections
        for conn_id in connection_ids:
            await self.connection_manager.remove_connection(conn_id)
        
        # Clear message buffer
        await self.message_buffer.clear_session(session_id)
        
        # Terminate Claude process
        await self._terminate_claude_process(session_id)
        
        log.info(f"Destroyed session {session_id} (explicit={explicit})")
    
    async def get_session(self, session_id: str) -> Optional[SessionInfo]:
        """Get session by ID."""
        async with self._lock:
            session = self._sessions.get(session_id)
            if session:
                session.last_activity = time.time()
            return session
    
    async def get_session_by_tab(self, tab_id: str) -> Optional[SessionInfo]:
        """Get session by tab ID."""
        async with self._lock:
            session_id = self._tab_sessions.get(tab_id)
            if session_id:
                return self._sessions.get(session_id)
            return None
    
    # === Connection Management ===
    
    async def attach_connection(self, session_id: str, connection_id: str) -> bool:
        """
        Attach a WebSocket connection to a session.
        
        Args:
            session_id: Session identifier
            connection_id: Connection identifier
            
        Returns:
            True if successful
        """
        session = await self.get_session(session_id)
        if not session:
            log.error(f"Session {session_id} not found")
            return False
        
        # Attach to connection manager
        success = await self.connection_manager.attach_to_session(connection_id, session_id)
        
        if success:
            # Update session state
            async with self._lock:
                session.state = SessionState.ACTIVE
                session.last_activity = time.time()
            
            # Send any buffered messages
            await self._replay_messages(session_id, connection_id)
        
        return success
    
    async def detach_connection(self, connection_id: str):
        """
        Detach a connection from its session.

        Args:
            connection_id: Connection identifier
        """
        session_id = await self.connection_manager.detach_from_session(connection_id)

        if session_id:
            # Check if session has any remaining connections
            connections = await self.connection_manager.get_session_connections(session_id)
            if not connections:
                # No active connections, mark as inactive
                async with self._lock:
                    session = self._sessions.get(session_id)
                    if session:
                        session.state = SessionState.INACTIVE
                        # Note: We keep the proxy route registered even when inactive
                        # Routes are only cleaned up when session is destroyed
    
    # === Message Management ===
    
    async def send_message(self, session_id: str, message: Dict) -> Tuple[int, int]:
        """
        Send a message to a session.
        Credentials are passed globally with each message to proxy.

        Args:
            session_id: Session identifier
            message: Message to send

        Returns:
            Tuple of (successful sends, failed sends)
        """
        # Get sequence number from AckManager if available
        if hasattr(self, 'ack_manager') and self.ack_manager:
            seq = await self.ack_manager.get_next_broker_seq(session_id)
        else:
            # Fallback to buffer's sequence
            msg = await self.message_buffer.add_message(session_id, message)
            seq = msg.seq

        # Add to buffer if not already added
        if not (hasattr(self, 'ack_manager') and self.ack_manager):
            msg = await self.message_buffer.add_message(session_id, message)
        else:
            # Still add to buffer for persistence
            await self.message_buffer.add_message(session_id, message)

        # Add sequence number to message
        message['seq'] = seq

        # Send via connection manager
        successful, failed = await self.connection_manager.send_to_session(session_id, message)

        # If no active connections, message remains in buffer for replay
        if successful == 0 and failed == 0:
            log.warning(f"⚠️ No active connections for session {session_id}, message buffered (seq={seq})")
        elif failed > 0:
            log.warning(f"⚠️ Failed to send to {failed} connections for session {session_id} (seq={seq})")
        else:
            log.debug(f"✅ Sent message to {successful} connections for session {session_id} (seq={seq})")

        return successful, failed

    async def send_message_batch(self, session_id: str, events: List[Dict], message_type: str = 'conversation_events_batch', tab_id: str = None) -> Tuple[int, int]:
        """
        Send a batch of events to a session in a single message.

        Efficient for loading conversations - sends all events in one websocket message.

        Args:
            session_id: Session identifier
            events: List of event dicts to send
            message_type: Type of batch message (default: 'conversation_events_batch')
            tab_id: Optional tab ID for iOS routing

        Returns:
            Tuple of (successful sends, failed sends)
        """
        # Get sequence number from AckManager
        if hasattr(self, 'ack_manager') and self.ack_manager:
            seq = await self.ack_manager.get_next_broker_seq(session_id)
        else:
            # Fallback to buffer's sequence
            seq = self.message_buffer._get_next_seq(session_id)

        # Create batch message
        batch_message = {
            'type': message_type,
            'events': events,
            'eventCount': len(events),
            'seq': seq
        }

        # Add tabId for iOS routing if provided
        if tab_id:
            batch_message['tabId'] = tab_id

        # Add to buffer for persistence
        await self.message_buffer.add_message(session_id, batch_message)

        # Send via connection manager
        successful, failed = await self.connection_manager.send_to_session(session_id, batch_message)

        # If no active connections, message remains in buffer for replay
        if successful == 0 and failed == 0:
            log.debug(f"No active connections for session {session_id}, batch message buffered")

        log.debug(f"Sent batch of {len(events)} events to session {session_id} (seq={seq})")
        return successful, failed

    async def acknowledge_message(self, session_id: str, seq: int) -> bool:
        """
        Acknowledge receipt of a message.
        
        Args:
            session_id: Session identifier
            seq: Sequence number to acknowledge
            
        Returns:
            True if acknowledged
        """
        return await self.message_buffer.acknowledge_message(session_id, seq)
    
    async def acknowledge_up_to(self, session_id: str, seq: int) -> int:
        """
        Acknowledge all messages up to sequence number.
        
        Args:
            session_id: Session identifier
            seq: Sequence number to acknowledge up to
            
        Returns:
            Number of messages acknowledged
        """
        return await self.message_buffer.acknowledge_up_to(session_id, seq)
    
    async def _replay_messages(self, session_id: str, connection_id: str):
        """
        Replay buffered messages to a reconnected client.
        Sends explicit sync_status messages at start and end of replay.

        Args:
            session_id: Session identifier
            connection_id: Connection identifier
        """
        # Get connection info
        conn_info = self.connection_manager.get_connection(connection_id)
        if not conn_info:
            return

        # Get session to access tab_id
        session = await self.get_session(session_id)
        if not session:
            return

        # Get last acknowledged sequence from session's ack_manager (persistent state)
        # NOT from connection's client_info (ephemeral, lost on reconnect)
        if hasattr(self, 'ack_manager') and self.ack_manager:
            ack_state = await self.ack_manager.get_or_create_state(session_id)
            last_ack = ack_state.ios_last_acked
            log.info(f"Using session ack state: ios_last_acked={last_ack} for session {session_id}")
        else:
            # Fallback: no ack manager, replay all
            last_ack = -1
            log.warning(f"No ack_manager available, replaying all messages for session {session_id}")

        # Get messages to replay
        messages = await self.message_buffer.get_messages_since(session_id, last_ack)

        if messages:
            log.info(f"Replaying {len(messages)} messages to {connection_id}")

            # Send sync_status at start of replay (is_synced=false)
            if hasattr(self, 'ack_manager') and self.ack_manager:
                sync_start_seq = await self.ack_manager.get_next_broker_seq(session_id)
                sync_status = await self.ack_manager.get_sync_status(session_id)
                await conn_info.websocket.send(json.dumps({
                    'type': 'sync_status',
                    'tabId': session.tab_id,
                    'sync': {
                        'is_synced': False,
                        'broker_to_ios': sync_status['broker_to_ios'],
                        'ios_to_broker': sync_status['ios_to_broker']
                    },
                    'missed_count': len(messages),
                    'seq': sync_start_seq
                }))
                log.info(f"Sent sync_status (start replay): missed_count={len(messages)}, is_synced=False")

            # Send each message
            for msg in messages:
                try:
                    message_with_seq = msg.content.copy()
                    message_with_seq['seq'] = msg.seq
                    message_with_seq['replay'] = True  # Mark as replay
                    if not conn_info.websocket or not websocket_is_open(conn_info.websocket):
                        log.warning(f"Cannot replay message {msg.seq} - connection {connection_id} closed")
                        break

                    await conn_info.websocket.send(json.dumps(message_with_seq))
                except Exception as e:
                    log.error(f"Failed to replay message {msg.seq}: {e}")
                    break

            # Send sync_status at end of replay (is_synced=true)
            if hasattr(self, 'ack_manager') and self.ack_manager:
                sync_end_seq = await self.ack_manager.get_next_broker_seq(session_id)
                sync_status = await self.ack_manager.get_sync_status(session_id)
                await conn_info.websocket.send(json.dumps({
                    'type': 'sync_status',
                    'tabId': session.tab_id,
                    'sync': {
                        'is_synced': True,  # Replay complete
                        'broker_to_ios': sync_status['broker_to_ios'],
                        'ios_to_broker': sync_status['ios_to_broker']
                    },
                    'missed_count': 0,
                    'seq': sync_end_seq
                }))
                log.info(f"Sent sync_status (end replay): is_synced=True")
        else:
            log.debug(f"No messages to replay for {connection_id}")
    
    # === Background Tasks ===
    
    async def _cleanup_loop(self):
        """Background task to clean up inactive sessions."""
        while True:
            try:
                await asyncio.sleep(self.cleanup_interval)
                
                if self.session_timeout > 0:  # Only cleanup if timeout is set
                    await self._cleanup_inactive_sessions()
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error(f"Error in cleanup loop: {e}")
    
    async def _cleanup_inactive_sessions(self):
        """Clean up sessions that have been inactive too long."""
        now = time.time()
        sessions_to_cleanup = []
        
        async with self._lock:
            for session_id, session in self._sessions.items():
                if session.state == SessionState.INACTIVE:
                    if now - session.last_activity > self.session_timeout:
                        sessions_to_cleanup.append(session_id)
        
        # Cleanup outside lock
        for session_id in sessions_to_cleanup:
            log.info(f"Cleaning up inactive session {session_id}")
            await self.destroy_session(session_id, explicit=False)
    
    async def _process_messages_loop(self):
        """Background task to process queued messages."""
        # This would integrate with Claude SDK to process messages
        while True:
            try:
                await asyncio.sleep(0.1)  # Process frequently
                # Placeholder - will integrate with real message processing
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error(f"Error in message processor: {e}")
    
    # === Claude Process Management (Placeholders) ===
    
    async def _initialize_claude_process(self, session_id: str):
        """Initialize Claude process for session."""
        # Placeholder - will integrate with real Claude SDK
        self._claude_processes[session_id] = {
            'started_at': time.time(),
            'status': 'running'
        }
        log.info(f"Initialized Claude process for session {session_id}")
    
    async def _terminate_claude_process(self, session_id: str):
        """Terminate Claude process for session."""
        # Close the Claude CLI session if it exists
        if hasattr(self, 'claude_interface') and self.claude_interface:
            await self.claude_interface.close_session(session_id)

        # Clean up process tracking
        if session_id in self._claude_processes:
            del self._claude_processes[session_id]
            log.info(f"Terminated Claude process for session {session_id}")

    # === Proxy Route Management ===

    def _register_session_route(self, session_id: str):
        """
        Register a session-specific proxy route.

        Args:
            session_id: Session identifier
        """
        if not self.global_credentials:
            log.warning(f"No global credentials available for session {session_id} route registration")
            return

        from proxy.registry import register_route
        from proxy.config import ModelConfig

        session_token = f"kisuke-{session_id[:5]}"
        config = ModelConfig(
            provider=self.global_credentials.provider,
            base_url=self.global_credentials.base_url,
            api_key=self.global_credentials.api_key,
            model=self.global_credentials.model,
            auth_method=self.global_credentials.auth_method,
            extra_headers=self.global_credentials.extra_headers or {},
            azure_deployment=self.global_credentials.azure_deployment,
            azure_api_version=self.global_credentials.azure_api_version
        )
        register_route(session_token, config)
        log.info(f"Registered session route: token={session_token} provider={config.provider} model={config.model}")

    def _unregister_session_route(self, session_id: str):
        """
        Unregister a session-specific proxy route.

        Args:
            session_id: Session identifier
        """
        from proxy.registry import unregister_route

        session_token = f"kisuke-{session_id[:5]}"
        unregister_route(session_token)
        log.info(f"Unregistered session route: token={session_token}")

    # === Statistics ===
    
    def get_stats(self) -> Dict:
        """Get session manager statistics."""
        return {
            'total_sessions': len(self._sessions),
            'active_sessions': sum(1 for s in self._sessions.values() if s.state == SessionState.ACTIVE),
            'inactive_sessions': sum(1 for s in self._sessions.values() if s.state == SessionState.INACTIVE),
            'total_tabs': len(self._tab_sessions),
            'claude_processes': len(self._claude_processes),
            'connection_stats': self.connection_manager.get_stats(),
            'buffer_stats': self.message_buffer.get_all_stats()
        }
    
    def get_all_sessions(self) -> List[Dict]:
        """Get all session information."""
        sessions = []
        for session in self._sessions.values():
            sessions.append({
                'session_id': session.session_id,
                'tab_id': session.tab_id,
                'state': session.state.value,
                'created_at': session.created_at,
                'last_activity': session.last_activity,
                'workdir': session.workdir,
                'permission_mode': session.permission_mode
            })
        return sessions
