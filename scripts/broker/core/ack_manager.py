"""
Acknowledgement manager for reliable message delivery.

Handles bidirectional ACK logic between iOS and Broker.
Ensures no message loss during network interruptions.
"""
import asyncio
import logging
from typing import Dict, Optional, Tuple, Set, Any
from dataclasses import dataclass, field
import time

log = logging.getLogger(__name__)


@dataclass
class AckState:
    """Track ACK state for a session."""
    # Broker → iOS tracking
    broker_to_ios_seq: int = 0  # Next seq to send
    ios_last_acked: int = -1    # Last seq iOS acknowledged

    # iOS → Broker tracking
    ios_to_broker_seq: int = 0  # Next expected from iOS
    broker_last_sent_ack: int = -1  # Last ACK we sent to iOS

    # Pending ACKs
    pending_broker_to_ios: Set[int] = field(default_factory=set)
    pending_ios_to_broker: Set[int] = field(default_factory=set)

    # Message buffering for sequential processing
    # Stores messages that arrived out of order: seq -> message_data
    buffered_messages: Dict[int, Any] = field(default_factory=dict)

    # Timing
    last_ios_activity: float = field(default_factory=time.time)
    last_sync_sent: float = 0

    def get_sync_status(self) -> Dict:
        """Get current sync status."""
        return {
            "broker_to_ios": {
                "next_seq": self.broker_to_ios_seq,
                "last_acked": self.ios_last_acked,
                "pending_count": len(self.pending_broker_to_ios)
            },
            "ios_to_broker": {
                "next_seq": self.ios_to_broker_seq,
                "last_sent_ack": self.broker_last_sent_ack,
                "pending_count": len(self.pending_ios_to_broker)
            },
            "is_synced": len(self.pending_broker_to_ios) == 0 and len(self.pending_ios_to_broker) == 0
        }


class AckManager:
    """
    Manages acknowledgement state for all sessions.

    Responsibilities:
    - Track sequence numbers bidirectionally
    - Monitor pending acknowledgements
    - Generate ACK messages
    - Determine sync status
    """

    def __init__(self):
        """Initialize ACK manager."""
        self._states: Dict[str, AckState] = {}
        self._lock = asyncio.Lock()

    async def get_or_create_state(self, session_id: str) -> AckState:
        """Get or create ACK state for a session."""
        async with self._lock:
            if session_id not in self._states:
                self._states[session_id] = AckState()
                log.debug(f"Created ACK state for session {session_id}")
            return self._states[session_id]

    # === Broker → iOS Methods ===

    async def get_next_broker_seq(self, session_id: str) -> int:
        """
        Get next sequence number for broker → iOS message.

        Returns:
            Next sequence number to use
        """
        state = await self.get_or_create_state(session_id)
        async with self._lock:
            seq = state.broker_to_ios_seq
            state.broker_to_ios_seq += 1
            state.pending_broker_to_ios.add(seq)
            log.debug(f"Allocated broker seq {seq} for session {session_id}")
            return seq

    async def ack_from_ios(self, session_id: str, seq: int) -> int:
        """
        Process ACK from iOS for broker message.

        Args:
            session_id: Session identifier
            seq: Sequence number being acknowledged

        Returns:
            Number of messages acknowledged
        """
        state = await self.get_or_create_state(session_id)
        async with self._lock:
            count = 0
            # ACK this and all earlier sequences
            to_remove = [s for s in state.pending_broker_to_ios if s <= seq]
            for s in to_remove:
                state.pending_broker_to_ios.discard(s)
                count += 1

            if seq > state.ios_last_acked:
                state.ios_last_acked = seq
                log.info(f"iOS acknowledged up to seq {seq} for session {session_id} ({count} messages)")

            state.last_ios_activity = time.time()
            return count

    # === iOS → Broker Methods ===

    async def reset_ios_tracking(self, session_id: str):
        """
        Reset iOS→Broker sequence tracking when iOS reconnects.
        iOS will start sending from seq=1 after reconnect.
        """
        state = await self.get_or_create_state(session_id)
        async with self._lock:
            log.info(f"Resetting iOS→Broker tracking for session {session_id}")
            state.ios_to_broker_seq = 0
            state.broker_last_sent_ack = -1
            state.pending_ios_to_broker.clear()

    async def process_ios_message(self, session_id: str, ios_seq: Optional[int] = None, message_data: Any = None) -> list:
        """
        Process incoming iOS message with sequential ordering guarantee.
        Messages are buffered if they arrive out of order.

        Args:
            session_id: Session identifier
            ios_seq: Sequence from iOS (if provided)
            message_data: Optional message data to buffer (for future use)

        Returns:
            List of (seq, is_duplicate) tuples ready to be ACK'd in order.
            Empty list if message was buffered waiting for earlier messages.
        """
        state = await self.get_or_create_state(session_id)
        async with self._lock:
            if ios_seq is None:
                # iOS didn't provide seq, assign one
                ios_seq = state.ios_to_broker_seq
                state.ios_to_broker_seq += 1

            state.last_ios_activity = time.time()

            # Check if this is a duplicate (already processed)
            if ios_seq <= state.broker_last_sent_ack:
                log.debug(f"Duplicate message seq {ios_seq} for session {session_id} (last_ack={state.broker_last_sent_ack})")
                return [(ios_seq, True)]  # Duplicate - ACK again but mark as dup

            # Check if this is the next expected message
            next_expected = state.broker_last_sent_ack + 1

            if ios_seq == next_expected:
                # This is the next expected message - process it
                ready_messages = []

                # Add this message
                state.broker_last_sent_ack = ios_seq
                ready_messages.append((ios_seq, False))
                log.debug(f"Processing sequential message seq {ios_seq} for session {session_id}")

                # Check buffer for consecutive messages
                while True:
                    next_seq = state.broker_last_sent_ack + 1
                    if next_seq in state.buffered_messages:
                        # Found buffered message that's now ready
                        state.buffered_messages.pop(next_seq)
                        state.broker_last_sent_ack = next_seq
                        ready_messages.append((next_seq, False))
                        log.info(f"✅ Processed buffered message seq {next_seq} for session {session_id}")
                    else:
                        break

                return ready_messages

            else:
                # Gap detected - buffer this message
                state.buffered_messages[ios_seq] = message_data
                log.warning(f"⚠️ Gap detected: received seq {ios_seq}, expected {next_expected} - buffering for session {session_id}")
                log.info(f"   Buffered messages: {sorted(state.buffered_messages.keys())}")
                return []  # Don't ACK yet - waiting for earlier messages

    # === Sync Status Methods ===

    async def get_sync_status(self, session_id: str) -> Dict:
        """
        Get synchronization status for a session.

        Returns:
            Dict with sync status information
        """
        state = await self.get_or_create_state(session_id)
        return state.get_sync_status()

    async def should_send_sync(self, session_id: str, interval: float = 5.0) -> bool:
        """
        Check if we should send a sync status message.

        Args:
            session_id: Session identifier
            interval: Minimum seconds between sync messages

        Returns:
            True if sync should be sent
        """
        state = await self.get_or_create_state(session_id)
        async with self._lock:
            now = time.time()
            # Send sync if: has pending AND enough time passed
            has_pending = len(state.pending_broker_to_ios) > 0
            time_passed = (now - state.last_sync_sent) > interval

            if has_pending and time_passed:
                state.last_sync_sent = now
                return True
            return False

    async def get_ios_reconnect_info(self, session_id: str, last_received_seq: int) -> Dict:
        """
        Get information needed for iOS reconnection.

        Args:
            session_id: Session identifier
            last_received_seq: Last seq iOS claims to have received

        Returns:
            Dict with reconnection info
        """
        state = await self.get_or_create_state(session_id)
        async with self._lock:
            # Determine what iOS has missed
            missed_count = 0
            if state.broker_to_ios_seq > 0:
                # Count messages iOS hasn't seen
                for seq in range(last_received_seq + 1, state.broker_to_ios_seq):
                    missed_count += 1

            return {
                "last_received_seq": last_received_seq,
                "next_expected_seq": state.broker_to_ios_seq,
                "missed_count": missed_count,
                "has_pending": len(state.pending_broker_to_ios) > 0,
                "sync_status": state.get_sync_status()
            }

    async def reset_session(self, session_id: str):
        """Reset ACK state for a session (used for testing)."""
        async with self._lock:
            if session_id in self._states:
                del self._states[session_id]
                log.info(f"Reset ACK state for session {session_id}")

    def get_stats(self) -> Dict:
        """Get global ACK statistics (synchronous for monitoring)."""
        total_pending = 0
        total_sessions = len(self._states)

        for state in self._states.values():
            total_pending += len(state.pending_broker_to_ios)
            total_pending += len(state.pending_ios_to_broker)

        return {
            "sessions": total_sessions,
            "total_pending": total_pending,
            "states": {
                sid: state.get_sync_status()
                for sid, state in self._states.items()
            }
        }