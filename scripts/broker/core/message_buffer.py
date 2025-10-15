"""
Message buffering and replay system for reliable message delivery.
Ensures no messages are ever lost during iOS disconnections.
"""
import asyncio
import logging
from typing import Dict, List, Optional, Set, Tuple
from collections import deque
from dataclasses import dataclass, field
import time

from ..models import Message

log = logging.getLogger(__name__)


class MessageBuffer:
    """
    Thread-safe message buffer with replay capability.
    
    Key features:
    - Sequential message numbering per session
    - Message acknowledgment tracking
    - Replay of unacknowledged messages
    - Configurable buffer size and retention
    """
    
    def __init__(self, 
                 max_buffer_size: int = 10000,
                 retention_time: int = 3600):  # 1 hour default
        """
        Initialize message buffer.
        
        Args:
            max_buffer_size: Max messages per session
            retention_time: How long to keep acknowledged messages (seconds)
        """
        self.max_buffer_size = max_buffer_size
        self.retention_time = retention_time
        
        # Session message buffers
        self._buffers: Dict[str, deque[Message]] = {}
        
        # Next sequence numbers per session
        self._next_seq: Dict[str, int] = {}
        
        # Lock for thread safety
        self._lock = asyncio.Lock()
        
        # Cleanup task
        self._cleanup_task: Optional[asyncio.Task] = None
    
    async def start(self):
        """Start buffer cleanup task."""
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        log.info("Message buffer started")
    
    async def stop(self):
        """Stop buffer and cleanup."""
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
        log.info("Message buffer stopped")
    
    async def add_message(self, 
                          session_id: str,
                          content: Dict) -> Message:
        """
        Add a message to the buffer with sequence number.
        Credentials are passed globally with each message to proxy.
        
        Args:
            session_id: Session identifier
            content: Message content
            
        Returns:
            Message object with sequence number
        """
        async with self._lock:
            
            # Initialize buffer if needed
            if session_id not in self._buffers:
                self._buffers[session_id] = deque(maxlen=self.max_buffer_size)
                self._next_seq[session_id] = 0
            
            # Create message with sequence number (no credential needed - they're global)
            seq = self._next_seq[session_id]

            # Extract turn_id if present in content for tracking
            turn_id = None
            parent_turn_id = None
            if isinstance(content, dict):
                turn_id = content.get('turn_id')
                parent_turn_id = content.get('parent_turn_id')

            message = Message(
                seq=seq,
                content=content,
                turn_id=turn_id,
                parent_turn_id=parent_turn_id
            )
            
            # Add to buffer
            self._buffers[session_id].append(message)
            self._next_seq[session_id] = seq + 1
            
            log.debug(f"Added message seq={seq} for session {session_id}")
            return message
    
    async def acknowledge_message(self,
                                  session_id: str,
                                  seq: int) -> bool:
        """
        Mark a message as acknowledged.
        
        Args:
            session_id: Session identifier
            seq: Sequence number to acknowledge
            
        Returns:
            True if acknowledged, False if not found
        """
        async with self._lock:
            if session_id not in self._buffers:
                return False
            
            for msg in self._buffers[session_id]:
                if msg.seq == seq:
                    msg.acknowledged = True
                    log.debug(f"Acknowledged message seq={seq} for session {session_id}")
                    return True
            
            return False
    
    async def acknowledge_up_to(self,
                                session_id: str,
                                seq: int) -> int:
        """
        Acknowledge all messages up to and including sequence number.
        
        Args:
            session_id: Session identifier
            seq: Sequence number to acknowledge up to
            
        Returns:
            Number of messages acknowledged
        """
        async with self._lock:
            if session_id not in self._buffers:
                return 0
            
            count = 0
            for msg in self._buffers[session_id]:
                if msg.seq <= seq and not msg.acknowledged:
                    msg.acknowledged = True
                    count += 1
            
            if count > 0:
                log.info(f"Acknowledged {count} messages up to seq={seq} for session {session_id}")
            
            return count
    
    async def get_unacknowledged(self,
                                 session_id: str,
                                 since_seq: int = -1) -> List[Message]:
        """
        Get all unacknowledged messages after a sequence number.
        
        Args:
            session_id: Session identifier
            since_seq: Get messages after this sequence (-1 for all)
            
        Returns:
            List of unacknowledged messages
        """
        async with self._lock:
            if session_id not in self._buffers:
                return []
            
            unacked = []
            for msg in self._buffers[session_id]:
                if not msg.acknowledged and msg.seq > since_seq:
                    unacked.append(msg)
            
            log.debug(f"Found {len(unacked)} unacknowledged messages for session {session_id}")
            return unacked
    
    async def get_messages_since(self,
                                 session_id: str,
                                 since_seq: int) -> List[Message]:
        """
        Get all messages after a sequence number (for replay).
        
        Args:
            session_id: Session identifier
            since_seq: Get messages after this sequence
            
        Returns:
            List of messages for replay
        """
        async with self._lock:
            if session_id not in self._buffers:
                return []
            
            messages = []
            for msg in self._buffers[session_id]:
                if msg.seq > since_seq:
                    messages.append(msg)
            
            log.info(f"Replaying {len(messages)} messages since seq={since_seq} for session {session_id}")
            return messages
    
    async def clear_session(self, session_id: str) -> int:
        """
        Clear all messages for a session.
        
        Args:
            session_id: Session identifier
            
        Returns:
            Number of messages cleared
        """
        async with self._lock:
            if session_id in self._buffers:
                count = len(self._buffers[session_id])
                del self._buffers[session_id]
                del self._next_seq[session_id]
                log.info(f"Cleared {count} messages for session {session_id}")
                return count
            return 0
    
    async def get_session_stats(self, session_id: str) -> Dict:
        """
        Get buffer statistics for a session.
        
        Args:
            session_id: Session identifier
            
        Returns:
            Statistics dictionary
        """
        async with self._lock:
            if session_id not in self._buffers:
                return {
                    "exists": False,
                    "total": 0,
                    "acknowledged": 0,
                    "unacknowledged": 0,
                    "next_seq": 0
                }
            
            buffer = self._buffers[session_id]
            acked = sum(1 for msg in buffer if msg.acknowledged)
            
            return {
                "exists": True,
                "total": len(buffer),
                "acknowledged": acked,
                "unacknowledged": len(buffer) - acked,
                "next_seq": self._next_seq.get(session_id, 0),
                "oldest_seq": buffer[0].seq if buffer else None,
                "newest_seq": buffer[-1].seq if buffer else None
            }
    
    async def _cleanup_loop(self):
        """Background task to clean up old acknowledged messages."""
        while True:
            try:
                await asyncio.sleep(60)  # Run every minute
                await self._cleanup_old_messages()
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error(f"Error in cleanup loop: {e}")
    
    async def _cleanup_old_messages(self):
        """Remove old acknowledged messages to save memory."""
        async with self._lock:
            now = time.time()
            total_removed = 0
            
            for session_id, buffer in self._buffers.items():
                # Create new deque without old acked messages
                new_buffer = deque(maxlen=self.max_buffer_size)
                removed = 0
                
                for msg in buffer:
                    # Keep if: not acked OR recent OR is recent unacked
                    if (not msg.acknowledged or 
                        (now - msg.timestamp) < self.retention_time or
                        msg.seq > (self._next_seq[session_id] - 100)):  # Keep last 100 for safety
                        new_buffer.append(msg)
                    else:
                        removed += 1
                
                if removed > 0:
                    self._buffers[session_id] = new_buffer
                    total_removed += removed
            
            if total_removed > 0:
                log.info(f"Cleaned up {total_removed} old acknowledged messages")
    
    def get_all_stats(self) -> Dict:
        """Get global buffer statistics (synchronous for monitoring)."""
        stats = {
            "sessions": len(self._buffers),
            "total_messages": sum(len(b) for b in self._buffers.values()),
            "total_unacked": sum(
                sum(1 for m in b if not m.acknowledged) 
                for b in self._buffers.values()
            )
        }
        return stats