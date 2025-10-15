"""
Data models and types for Kisuke Broker.
"""
from dataclasses import dataclass, field
from typing import Dict, Any, Optional, Set, List
from enum import Enum
import time
import uuid

from .utils import websocket_is_open

# Message types for broker communication
class MessageType(str, Enum):
    """Types of messages handled by the broker."""
    ROUTES = "routes"
    START = "start"
    SEND = "send"
    PERMISSION_RESPONSE = "permission_response"
    HEALTH = "health"
    STATUS = "status"
    SHUTDOWN = "shutdown"
    SET_ACTIVE_ROUTE = "set_active_route"
    SET_STABLE_ROUTE = "set_stable_route"
    SYNC = "sync"
    RESEND_MESSAGE = "resend_message"
    RESPONSE_ACK = "response_ack"
    UPDATE_PERMISSION_MODE = "update_permission_mode"
    UPDATE_PERMISSION_RULES = "update_permission_rules"
    UPDATE_CREDENTIALS = "update_credentials"  # iOS updates global credentials
    REQUEST_CREDENTIALS = "request_credentials"  # Broker requests credentials from iOS
    EDIT_MESSAGE = "edit_message"  # iOS edits a previous message (branching)
    MESSAGE_RECEIVED_ACK = "message_received_ack"  # Broker ACKs iOS message receipt
    SYNC_STATUS = "sync_status"  # Sync state between broker and iOS
    INTERRUPT = "interrupt"  # iOS requests to interrupt current Claude operation
    SET_PERMISSION_MODE = "set_permission_mode"  # iOS changes Claude permission mode at runtime
    REQUEST_CONVERSATIONS = "request_conversations"  # iOS requests conversation history for a project
    LOAD_CONVERSATION = "load_conversation"  # iOS requests to load a specific conversation

# Session states (from new implementation)
class SessionState(str, Enum):
    """Session lifecycle states."""
    CREATED = "created"
    INITIALIZING = "initializing"
    READY = "ready"
    ACTIVE = "active"
    INACTIVE = "inactive"  # No active connections
    HIBERNATED = "hibernated"  # Suspended for resource saving
    ERROR = "error"
    TERMINATED = "terminated"

# API Credentials Management (iOS is source of truth)
@dataclass
class ClaudeApiCredentials:
    """API credentials provided by iOS for Claude communication."""
    credential_id: str  # Unique ID for this credential set
    provider: str       # "anthropic", "openai", etc.
    model: str         # "claude-3-sonnet", "gpt-4", etc.  
    base_url: str      # API endpoint URL
    api_key: str       # API key from iOS
    auth_method: Optional[str] = None
    extra_headers: Dict[str, str] = field(default_factory=dict)
    azure_deployment: Optional[str] = None
    azure_api_version: Optional[str] = None

# Session info (sessions don't store credentials - they're global)
@dataclass
class SessionInfo:
    """
    Core session information.
    Sessions are persistent and survive WebSocket disconnections.
    Credentials are stored globally in broker, not per session.
    """
    session_id: str
    tab_id: str  # iOS tab identifier
    created_at: float = field(default_factory=time.time)

    # Claude state
    claude_session_id: Optional[str] = None
    claude_process_id: Optional[int] = None
    workdir: str = "/tmp"
    system_prompt: Optional[str] = None

    # Session state
    state: SessionState = SessionState.CREATED
    last_activity: float = field(default_factory=time.time)
    message_count: int = 0

    # Permissions (runtime modifiable)
    permission_mode: str = "prompt"
    permission_rules: Dict[str, str] = field(default_factory=dict)

    # Message tracking (Broker → iOS)
    last_sent_seq: int = 0  # Last sequence number sent to iOS
    last_ack_seq: int = -1  # Last sequence acknowledged by iOS

    # Message tracking (iOS → Broker)
    last_received_from_ios_seq: int = -1  # Last sequence received from iOS
    ios_message_counter: int = 0  # Track iOS messages for ACK

    # Message edit/branching support (iOS provides messageUuid)
    branch_point_uuid: Optional[str] = None  # UUID where last branch/edit occurred (from iOS)
    original_session_id: Optional[str] = None  # Original session ID if this is a branched session

# Message with sequence numbers (from new implementation)
@dataclass
class Message:
    """
    Message with sequence number for reliable delivery.
    Credentials are passed globally with each message to proxy.
    """
    seq: int  # Sequence number for ordering
    content: Dict[str, Any]  # Actual message content
    timestamp: float = field(default_factory=time.time)
    acknowledged: bool = False
    attempts: int = 0
    id: str = field(default_factory=lambda: str(uuid.uuid4()))

    # Claude turn tracking for correlation
    turn_id: Optional[str] = None  # Claude's UUID for this turn
    parent_turn_id: Optional[str] = None  # For tool responses/follow-ups

# Connection info (from new implementation)
@dataclass
class ConnectionInfo:
    """Information about a WebSocket connection."""
    connection_id: str
    websocket: Any  # WebSocketServerProtocol
    session_id: Optional[str] = None
    connected_at: float = field(default_factory=time.time)
    last_activity: float = field(default_factory=time.time)
    client_info: Dict = field(default_factory=dict)  # User agent, iOS version, etc.
    
    def to_dict(self) -> Dict:
        """Serialize for monitoring/debugging."""
        return {
            "connection_id": self.connection_id,
            "session_id": self.session_id,
            "connected_at": self.connected_at,
            "last_activity": self.last_activity,
            "client_info": self.client_info,
            "is_alive": websocket_is_open(self.websocket) if self.websocket else False
        }

# Error handling
class ErrorCode(str, Enum):
    """Error codes for iOS client."""
    MISSING_SESSION_ID = "missing_session_id"  # Deprecated - use MISSING_TAB_ID
    MISSING_TAB_ID = "missing_tab_id"
    MISSING_CONTENT = "missing_content"
    NO_ACTIVE_ROUTE = "no_active_route"
    SESSION_NOT_FOUND = "session_not_found"
    INVALID_ROUTE_TOKEN = "invalid_route_token"
    CLAUDE_SEND_FAILED = "claude_send_failed"
    ROUTE_VALIDATION_FAILED = "route_validation_failed"
    SYSTEM_ERROR = "system_error"

# Route management (simplified - no backward compatibility needed)


# Broker state (global credentials for all sessions)
@dataclass
class BrokerState:
    """Global broker state."""
    sessions: Dict[str, SessionInfo] = field(default_factory=dict)
    global_credentials: Optional[ClaudeApiCredentials] = None  # Global credentials from iOS
    running: bool = False
