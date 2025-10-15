"""
WebSocket connection manager that decouples connections from sessions.
Handles brittle iOS connections with automatic session attachment/detachment.
"""
import asyncio
import logging
import json
import time
from typing import Dict, Set, List, Optional, Tuple
from dataclasses import dataclass, field
import weakref

from ..models import ConnectionInfo
from ..utils import websocket_is_open

log = logging.getLogger(__name__)


class ConnectionManager:
    """
    Manages WebSocket connections independently of sessions.
    
    Key features:
    - Sessions persist beyond WebSocket lifecycle
    - Multiple WebSockets can attach to same session (multi-device)
    - Automatic session reattachment on reconnect
    - Connection pooling and management
    """
    
    def __init__(self, 
                 max_connections_per_session: int = 3,
                 connection_timeout: int = 300,  # 5 minutes
                 cleanup_interval: int = 30):  # 30 seconds
        """
        Initialize connection manager.
        
        Args:
            max_connections_per_session: Max concurrent connections per session
            connection_timeout: Timeout for idle connections (seconds)
            cleanup_interval: Interval for cleanup task (seconds)
        """
        self.max_connections_per_session = max_connections_per_session
        self.connection_timeout = connection_timeout
        self.cleanup_interval = cleanup_interval
        
        # Connection tracking
        self._connections: Dict[str, ConnectionInfo] = {}
        
        # Session to connections mapping
        self._session_connections: Dict[str, Set[str]] = {}
        
        # Reverse mapping for quick lookups
        self._websocket_to_connection: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
        
        # Lock for thread safety
        self._lock = asyncio.Lock()
        
        # Cleanup task
        self._cleanup_task: Optional[asyncio.Task] = None
        
    async def start(self):
        """Start connection manager."""
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        log.info("Connection manager started")
    
    async def stop(self):
        """Stop connection manager."""
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
        
        # Close all connections
        async with self._lock:
            for conn_info in list(self._connections.values()):
                if conn_info.websocket and websocket_is_open(conn_info.websocket):
                    await conn_info.websocket.close()
        
        log.info("Connection manager stopped")
    
    async def add_connection(self, 
                            connection_id: str,
                            websocket: any,
                            client_info: Dict = None) -> ConnectionInfo:
        """
        Add a new WebSocket connection.
        
        Args:
            connection_id: Unique connection identifier
            websocket: WebSocket connection object
            client_info: Optional client information
            
        Returns:
            ConnectionInfo object
        """
        async with self._lock:
            # Check if connection already exists
            if connection_id in self._connections:
                log.warning(f"Connection {connection_id} already exists")
                return self._connections[connection_id]
            
            # Create connection info
            conn_info = ConnectionInfo(
                connection_id=connection_id,
                websocket=websocket,
                client_info=client_info or {}
            )
            
            # Store connection
            self._connections[connection_id] = conn_info
            self._websocket_to_connection[websocket] = conn_info
            
            log.info(f"Added connection {connection_id}")
            return conn_info
    
    async def update_client_info(self, connection_id: str, info_updates: Dict) -> bool:
        """
        Update client info for a connection.

        Args:
            connection_id: Connection identifier
            info_updates: Dict of updates to merge into client_info

        Returns:
            True if updated successfully
        """
        async with self._lock:
            conn_info = self._connections.get(connection_id)
            if conn_info:
                conn_info.client_info.update(info_updates)
                log.debug(f"Updated client info for {connection_id}: {info_updates}")
                return True
            return False

    async def remove_connection(self, connection_id: str) -> Optional[str]:
        """
        Remove a WebSocket connection.

        Args:
            connection_id: Connection identifier

        Returns:
            Session ID if connection was attached to a session
        """
        async with self._lock:
            conn_info = self._connections.pop(connection_id, None)
            if not conn_info:
                return None
            
            session_id = conn_info.session_id
            
            # Remove from session mapping
            if session_id and session_id in self._session_connections:
                self._session_connections[session_id].discard(connection_id)
                if not self._session_connections[session_id]:
                    del self._session_connections[session_id]
            
            log.info(f"Removed connection {connection_id} (session: {session_id})")
            return session_id
    
    async def attach_to_session(self,
                               connection_id: str,
                               session_id: str) -> bool:
        """
        Attach a connection to a session.
        Supports multi-session connections (iOS uses one WebSocket for all tabs).

        Args:
            connection_id: Connection identifier
            session_id: Session identifier

        Returns:
            True if successful, False otherwise
        """
        oldest_to_close = None

        async with self._lock:
            # Get connection
            conn_info = self._connections.get(connection_id)
            if not conn_info:
                log.error(f"Connection {connection_id} not found")
                return False

            # Check max connections per session
            if session_id in self._session_connections:
                if len(self._session_connections[session_id]) >= self.max_connections_per_session:
                    log.warning(f"Session {session_id} has max connections")
                    # Mark oldest connection for closure (will close outside lock)
                    oldest_to_close = min(self._session_connections[session_id],
                                        key=lambda cid: self._connections[cid].connected_at)

            # NOTE: Do NOT detach from previous session!
            # iOS uses a single WebSocket for all tabs, so one connection must support multiple sessions.
            # The connection_info.session_id field tracks the "primary" session for logging,
            # but _session_connections is the source of truth for multi-session routing.

            # Update primary session ID (for logging/debugging only)
            if conn_info.session_id != session_id:
                log.info(f"Connection {connection_id} now also serving session {session_id} (was: {conn_info.session_id})")
            conn_info.session_id = session_id
            conn_info.last_activity = time.time()

            if session_id not in self._session_connections:
                self._session_connections[session_id] = set()
            self._session_connections[session_id].add(connection_id)

            log.info(f"Attached connection {connection_id} to session {session_id}")
        
        # Close oldest connection outside lock if needed
        if oldest_to_close:
            await self._close_connection(oldest_to_close)
        
        return True
    
    async def detach_from_session(self, connection_id: str) -> Optional[str]:
        """
        Detach a connection from its session.
        
        Args:
            connection_id: Connection identifier
            
        Returns:
            Session ID if was attached
        """
        async with self._lock:
            conn_info = self._connections.get(connection_id)
            if not conn_info or not conn_info.session_id:
                return None
            
            session_id = conn_info.session_id
            conn_info.session_id = None
            
            # Remove from session mapping
            if session_id in self._session_connections:
                self._session_connections[session_id].discard(connection_id)
                if not self._session_connections[session_id]:
                    del self._session_connections[session_id]
            
            log.info(f"Detached connection {connection_id} from session {session_id}")
            return session_id
    
    async def get_session_connections(self, session_id: str) -> List[ConnectionInfo]:
        """
        Get all connections for a session.
        
        Args:
            session_id: Session identifier
            
        Returns:
            List of ConnectionInfo objects
        """
        async with self._lock:
            conn_ids = self._session_connections.get(session_id, set())
            connections = []
            for conn_id in conn_ids:
                conn_info = self._connections.get(conn_id)
                if conn_info and conn_info.websocket and websocket_is_open(conn_info.websocket):
                    connections.append(conn_info)
            return connections
    
    async def send_to_session(self, 
                            session_id: str,
                            message: Dict) -> Tuple[int, int]:
        """
        Send message to all connections of a session.
        
        Args:
            session_id: Session identifier
            message: Message to send
            
        Returns:
            Tuple of (successful sends, failed sends)
        """
        # Get ALL connections for session (including closed)
        async with self._lock:
            conn_ids = self._session_connections.get(session_id, set())
            connections = []
            for conn_id in conn_ids:
                conn_info = self._connections.get(conn_id)
                if conn_info:
                    connections.append(conn_info)
        
        if not connections:
            log.warning(f"⚠️ No connections found for session {session_id} - message will be buffered")
            return (0, 0)
        
        message_str = json.dumps(message)
        successful = 0
        failed = 0
        to_cleanup = []
        
        for conn_info in connections:
            try:
                # Check if WebSocket is open before sending
                if not conn_info.websocket or not websocket_is_open(conn_info.websocket):
                    log.warning(f"Connection {conn_info.connection_id} is closed")
                    failed += 1
                    to_cleanup.append(conn_info.connection_id)
                else:
                    await conn_info.websocket.send(message_str)
                    conn_info.last_activity = time.time()
                    successful += 1
            except Exception as e:
                log.error(f"Failed to send to connection {conn_info.connection_id}: {e}")
                failed += 1
                to_cleanup.append(conn_info.connection_id)
        
        # Mark dead connections for cleanup
        for conn_id in to_cleanup:
            asyncio.create_task(self._close_connection(conn_id))
        
        return (successful, failed)
    
    async def broadcast_to_all_sessions(self, message: Dict) -> Dict[str, Tuple[int, int]]:
        """
        Broadcast message to all sessions.
        
        Args:
            message: Message to broadcast
            
        Returns:
            Dict mapping session_id to (successful, failed) counts
        """
        results = {}
        for session_id in list(self._session_connections.keys()):
            results[session_id] = await self.send_to_session(session_id, message)
        return results
    
    def get_connection_by_websocket(self, websocket: any) -> Optional[ConnectionInfo]:
        """
        Get connection info by WebSocket object.
        
        Args:
            websocket: WebSocket object
            
        Returns:
            ConnectionInfo or None
        """
        return self._websocket_to_connection.get(websocket)
    
    def get_connection(self, connection_id: str) -> Optional[ConnectionInfo]:
        """
        Get connection info by ID.
        
        Args:
            connection_id: Connection identifier
            
        Returns:
            ConnectionInfo or None
        """
        return self._connections.get(connection_id)
    
    async def update_activity(self, connection_id: str):
        """
        Update last activity timestamp for a connection.
        
        Args:
            connection_id: Connection identifier
        """
        async with self._lock:
            conn_info = self._connections.get(connection_id)
            if conn_info:
                conn_info.last_activity = time.time()
    
    async def _close_connection(self, connection_id: str):
        """Close a specific connection."""
        conn_info = None
        async with self._lock:
            conn_info = self._connections.get(connection_id)
        
        # Close WebSocket outside of lock
        if conn_info and conn_info.websocket and websocket_is_open(conn_info.websocket):
            try:
                await conn_info.websocket.close()
            except:
                pass
        
        # Remove connection (this will acquire lock internally)
        await self.remove_connection(connection_id)
    
    async def _cleanup_loop(self):
        """Background task to clean up dead connections."""
        while True:
            try:
                await asyncio.sleep(self.cleanup_interval)
                await self._cleanup_dead_connections()
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error(f"Error in cleanup loop: {e}")
    
    async def _cleanup_dead_connections(self):
        """Remove dead or timed out connections."""
        async with self._lock:
            now = time.time()
            to_remove = []
            
            for conn_id, conn_info in self._connections.items():
                # Check if WebSocket is closed
                if not conn_info.websocket or not websocket_is_open(conn_info.websocket):
                    to_remove.append(conn_id)
                # Check for timeout (only if timeout is configured)
                elif self.connection_timeout > 0 and now - conn_info.last_activity > self.connection_timeout:
                    to_remove.append(conn_id)
                    log.info(f"Connection {conn_id} timed out")
        
        # Remove dead connections outside lock to prevent deadlock
        for conn_id in to_remove:
            await self.remove_connection(conn_id)
    
    def get_stats(self) -> Dict:
        """Get connection manager statistics."""
        return {
            "total_connections": len(self._connections),
            "active_sessions": len(self._session_connections),
            "connections_per_session": {
                session_id: len(conn_ids)
                for session_id, conn_ids in self._session_connections.items()
            },
            "unattached_connections": sum(
                1 for c in self._connections.values() if c.session_id is None
            )
        }
    
    def get_all_connections(self) -> List[Dict]:
        """Get all connection info for monitoring."""
        return [conn.to_dict() for conn in self._connections.values()]
