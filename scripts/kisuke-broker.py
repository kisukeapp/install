#!/usr/bin/env python3
"""Kisuke Broker - WebSocket relay between iOS clients and Claude Code SDK.

This module implements a WebSocket broker that manages connections between iOS
clients and the Claude Code SDK. It handles permission management, session
state, and message routing without modifying the SDK source code.

Key Features:
    - One WebSocket connection per iOS tab on configurable port
    - Efficient subprocess management with single CLI instance per tab
    - Built-in permission handling via SDK control channel interception
    - Compatible with existing iOS client JSON protocol
    - Session state management including plan mode transitions
    - Token usage tracking and context limits

Architecture:
    The broker subclasses ClaudeSDKClient to intercept control_request messages
    for permission handling. Each iOS client connection gets its own Session
    object that manages the Claude subprocess and message routing.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import socket
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, Optional, Set, Union

import contextlib
import websockets
from websockets.asyncio.server import ServerConnection as WebSocketServerProtocol

# Claude SDK ------------------------------------------------------------------
from claude_code_sdk import (
    AssistantMessage,
    ClaudeCodeOptions,
    ClaudeSDKClient,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from claude_code_sdk._errors import CLIConnectionError
from claude_code_sdk._internal.message_parser import parse_message

from kisuke_proxy import (
    start_proxy,
    register_route,
    unregister_route,
    UpstreamConfig,
    ModelConfig,
)

# Path utilities for cross-platform compatibility
class PathUtils:
    """Utilities for safe path handling and expansion.

    Provides cross-platform path operations including tilde expansion,
    environment variable expansion, and path validation.
    """

    @staticmethod
    def expand_path(path: str) -> str:
        """
        Expand a path to its absolute form, handling:
        - Tilde (~) expansion for home directory
        - Tilde with username (~username) expansion
        - Environment variable expansion
        - Relative to absolute path conversion

        Args:
            path: The path to expand (can be None, empty, or contain ~)

        Returns:
            Absolute expanded path, or current directory if input is invalid
        """
        if not path or not isinstance(path, str):
            return os.getcwd()

        # Remove leading/trailing whitespace
        path = path.strip()

        # Handle empty path after stripping
        if not path:
            return os.getcwd()

        # Expand user home directory (~) and username paths (~username)
        expanded = os.path.expanduser(path)

        # Expand environment variables (e.g., $HOME, %USERPROFILE%)
        expanded = os.path.expandvars(expanded)

        # Convert to absolute path and normalize (removes .., ., double slashes)
        absolute = os.path.abspath(expanded)

        return absolute

    @staticmethod
    def ensure_directory_exists(path: str) -> tuple[bool, str]:
        """
        Ensure a directory exists and is accessible.

        Args:
            path: The directory path to check

        Returns:
            Tuple of (exists: bool, expanded_path: str)
        """
        expanded = PathUtils.expand_path(path)

        if os.path.exists(expanded) and os.path.isdir(expanded):
            return True, expanded

        return False, expanded

    @staticmethod
    def is_safe_path(path: str, base_path: Optional[str] = None) -> bool:
        """
        Check if a path is safe (doesn't escape base directory via ..)

        Args:
            path: Path to check
            base_path: Optional base path to ensure we stay within

        Returns:
            True if path is safe, False otherwise
        """
        expanded = PathUtils.expand_path(path)

        if base_path:
            base_expanded = PathUtils.expand_path(base_path)
            # Verify path stays within base directory
            try:
                common = os.path.commonpath([expanded, base_expanded])
                return common == base_expanded
            except ValueError:
                # Different drives on Windows - not safe
                return False

        return True

# Configuration and logging setup
PORT           = int(os.getenv("PORT", 8765))  # WebSocket server port
CONTEXT_LIMIT  = 200_000                       # Maximum token budget per session
PROXY_HOST     = os.getenv("PROXY_HOST", "127.0.0.1")
PROXY_PORT     = int(os.getenv("PROXY_PORT", "8082"))

# One stable token that ALL tabs/processes will use to talk to the proxy
FIXED_ROUTE_TOKEN = "kisuke-active"  # internal; never changes at runtime

logging.basicConfig(
    level=logging.DEBUG if os.getenv("KISUKE_DEBUG") else logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
)
log = logging.getLogger("kisuke-broker-sdk")

# Silence noisy websockets keepalive ping/pong messages
logging.getLogger("websockets.protocol").setLevel(logging.INFO)
logging.getLogger("websockets.client").setLevel(logging.INFO)
logging.getLogger("websockets.server").setLevel(logging.INFO)
log.setLevel(logging.DEBUG if os.getenv("KISUKE_DEBUG") else logging.INFO)

log.debug("this should appear if KISUKE_DEBUG=1")


# Claude SDK client with integrated permission handling
class PermAwareClient(ClaudeSDKClient):
    """Claude SDK client with built-in permission request handling.

    Extends ClaudeSDKClient to intercept control_request messages for
    tool permissions, allowing custom permission flows without modifying
    the SDK source code. Handles the permission dialog between the iOS
    client and Claude SDK.

    Attributes:
        _perm_handler: Async function to handle permission requests.
        _session: Reference to the broker Session for this client.
        _init_session_id: Claude session ID captured from init message.
    """

    def __init__(
        self,
        *,
        permission_handler,                # Async callback for permission requests
        session = None,                    # Reference to parent ClaudeSession object
        broker = None,                     # Reference to broker for WebSocket access
        options: ClaudeCodeOptions | None = None,
    ):
        super().__init__(options=options)
        self._perm_handler = permission_handler
        self._session = session            # Reference to ClaudeSession
        self._broker = broker              # Reference to broker for WebSocket
        self._init_session_id = None       # Claude session ID from init

    async def receive_messages(self):
        """Receive and process messages from Claude CLI.

        Intercepts control_request messages for permission handling and
        forwards all other messages after parsing. Also captures session
        IDs and forwards tool results to iOS clients.

        Yields:
            Parsed messages from the Claude CLI.

        Raises:
            CLIConnectionError: If not connected to CLI.
        """
        if not self._transport:
            raise CLIConnectionError("Not connected – call connect() first")

        async for raw in self._transport.receive_messages():
            log.debug("RAW FROM CLI: %r", raw)

            # --- LIVE USAGE TICKS (cumulative) -----------------------------------------
            try:
                # Anthropic streams usage on message_start / message_delta; it's cumulative.
                usage = raw.get("usage") or (raw.get("message") or {}).get("usage")
                if usage and self._session and self._broker and self._broker.ios_websocket:
                    await self._broker.ios_websocket.send(json.dumps({
                        "event": "usage_tick",          # NEW: your iOS can render a live counter
                        "usage": usage,                 # e.g. {"input_tokens": 472, "output_tokens": 89, ...}
                        "model": (raw.get("message") or {}).get("model"),
                        "session_id": self._session.session_id,
                        "chat_id": self._session.session_id,  # Use session_id as chat_id
                    }))
                    log.debug("Emitted usage_tick: %s", usage)
            except Exception as _e:
                log.debug("usage_tick emit failed: %s", _e)
            # ---------------------------------------------------------------------------

            # Log tool use messages for debugging
            if raw.get("type") == "assistant":
                message = raw.get("message", {})
                content = message.get("content", []) if isinstance(message, dict) else raw.get("content", [])
                for item in content:
                    if item.get("type") == "tool_use":
                        log.debug("TOOL USE in assistant message: %s (id=%s)",
                                item.get("name"), item.get("id"))

            # Log tool result messages for debugging
            if raw.get("type") == "user":
                message = raw.get("message", {})
                content = message.get("content", []) if isinstance(message, dict) else []
                for item in content:
                    if item.get("type") == "tool_result":
                        log.debug("TOOL RESULT in user message: tool_use_id=%s, is_error=%s, content=%s",
                                item.get("tool_use_id"), item.get("is_error", False),
                                str(item.get("content", ""))[:100] + "..." if len(str(item.get("content", ""))) > 100 else item.get("content", ""))

                        # Forward tool result immediately to iOS
                        if self._session and self._broker and self._broker.ios_websocket:
                            try:
                                # Send tool result event
                                await self._broker.ios_websocket.send(json.dumps({
                                    "event": "tool_result",
                                    "tool_use_id": item.get("tool_use_id"),
                                    "content": item.get("content"),
                                    "is_error": item.get("is_error", False),
                                    "session_id": self._session.session_id,
                                    "chat_id": self._session.session_id  # Use session_id as chat_id
                                }))
                                log.debug("Forwarded tool result to iOS directly")
                            except Exception as e:
                                log.error("Failed to forward tool result directly: %s", e)

            # Capture session ID from init messages before parsing
            if raw.get("type") == "system" and raw.get("subtype") == "init":
                if "session_id" in raw:
                    # Store session ID in a place we can access later
                    self._init_session_id = raw["session_id"]
                    log.info("Captured Claude session ID from init: %s", self._init_session_id)

                    # Store in broker's session pool for resume functionality
                    if self._session and self._broker:
                        self._broker.session_pool.store_claude_session_id(
                            self._session.session_id, self._init_session_id
                        )
                        log.info("Stored Claude session ID in pool: %s -> %s", 
                                self._init_session_id, self._session.session_id)

            # Handle tool permission requests from Claude
            if (
                raw.get("type") == "control_request"
                and raw["request"].get("subtype") == "can_use_tool"
            ):
                req_id     = raw["request_id"]
                tool_name  = raw["request"]["tool_name"]
                tool_input = raw["request"]["input"]

                log.debug("CONTROL_REQUEST received for tool '%s' (req_id=%s)", tool_name, req_id)
                log.debug("   Input: %r", tool_input)

                try:
                    log.debug("Calling permission handler for tool=%s", tool_name)
                    # Forward permission request to iOS client via broker
                    resp: Dict[str, Any] = await self._perm_handler(
                        tool_name, tool_input, req_id
                    )

                    log.debug("Permission handler returned: %r", resp)

                    # Send permission response back to Claude CLI
                    # Note: CLI requires updatedInput field for allow responses
                    if resp.get("behavior") == "allow" and "updatedInput" not in resp:
                        # Add the original input as updatedInput
                        cli_response = {
                            "behavior": "allow",
                            "updatedInput": tool_input
                        }
                    else:
                        cli_response = resp

                    control_response = {
                        "type": "control_response",
                        "response": {
                            "subtype":   "success",
                            "request_id": req_id,
                            "response": cli_response
                        },
                    }
                    log.debug("Transformed response: %r -> %r", resp, cli_response)
                except Exception as error:
                    log.error("Permission handler error: %s", error)

                    # Emit error event to iOS for permission handler failure
                    # Note: We can't emit here as we don't have session context

                    # Send error response if permission handler fails
                    control_response = {
                        "type": "control_response",
                        "response": {
                            "subtype": "error",
                            "request_id": req_id,
                            "error": str(error)
                        }
                    }

                stdin_stream = getattr(self._transport, "_stdin_stream", None)
                if stdin_stream is None:
                    raise RuntimeError("stdin closed – cannot answer permission prompt")

                log.debug("Sending control_response: %s", json.dumps(control_response))
                await stdin_stream.send(json.dumps(control_response) + "\n")
                log.debug("Permission response sent, waiting for next message from SDK CLI...")
                continue

            # Skip control_response echoes to avoid loops
            if raw.get("type") == "control_response":
                continue

            # Forward all other message types after parsing
            yield parse_message(raw)

    async def receive_response(self):
        """
        Receive messages until and including a ResultMessage.
        This is useful for bounded operations like plan mode.
        """
        async for message in self.receive_messages():
            yield message
            if isinstance(message, ResultMessage):
                return


# Claude session management (independent of WebSocket connections)
@dataclass
class ClaudeSession:
    """Manages a Claude SDK subprocess session.
    
    These sessions persist across WebSocket reconnections to support
    iOS app lifecycle events (backgrounding, network changes, etc).
    
    Attributes:
        session_id: Stable session identifier (iOS tab ID)
        claude_session_id: Claude SDK's internal session ID for resume
        claude: Claude SDK client instance
        listener_task: Task for receiving Claude messages
    """
    
    session_id: str                                       # Stable session ID (iOS tab ID)
    claude_session_id: Optional[str] = None               # Claude SDK's session ID for resume
    claude: Optional[PermAwareClient] = None              # Claude SDK client instance
    listener_task: Optional[asyncio.Task] = None          # Task for receiving Claude messages
    last_activity: float = field(default_factory=lambda: asyncio.get_event_loop().time())
    
    # Session state
    is_active: bool = True                               # Whether session is active
    workdir: Optional[str] = None                        # Working directory
    permission_mode: str = "default"                     # Current permission mode

    allowed_tools: Set[str] = field(default_factory=set)  # Tools with permanent allow
    denied_tools:  Set[str] = field(default_factory=set)  # Tools with permanent deny

    used_tokens: int = 0                                  # Running token count
    pending_permissions: Dict[str, asyncio.Future] = field(default_factory=dict)  # Active permission requests
    pending_tools: Dict[str, Dict[str, Any]] = field(default_factory=dict)        # Active tool executions

    # Plan mode state management
    original_permission_mode: Optional[str] = None        # Mode to return to after plan
    in_plan_mode: bool = False                           # Currently in plan mode
    
    # Detailed usage tracking
    last_usage: Dict[str, int] = field(default_factory=lambda: {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    })
    
    # Message tracking for ACK protocol
    message_id_map: Dict[str, str] = field(default_factory=dict)  # iOS message ID -> Claude response ID
    pending_messages: Dict[str, Dict[str, Any]] = field(default_factory=dict)  # Message ID -> message data
    last_user_message_id: Optional[str] = None  # Track last user message for response correlation

    def ctx(self) -> Dict[str, Any]:
        """Generate context payload for outbound messages.

        Returns:
            Dictionary with session context including tokens and IDs.
        """
        return {
            "session_id":   self.session_id,
            "used_tokens":  self.used_tokens,
            "context_left": CONTEXT_LIMIT - self.used_tokens,
        }


class SessionPool:
    """Manages Claude sessions independently of WebSocket connections.
    
    Sessions persist across WebSocket reconnections to support iOS app
    lifecycle events. Sessions are kept alive for a timeout period after
    last use to allow resumption.
    """
    
    def __init__(self, session_timeout: float = 1800.0):  # 30 minutes default
        self.sessions: Dict[str, ClaudeSession] = {}      # tab_id -> ClaudeSession
        self.claude_id_map: Dict[str, str] = {}           # claude_session_id -> tab_id
        self.session_timeout = session_timeout
        self._cleanup_task: Optional[asyncio.Task] = None
    
    def get_session(self, session_id: str) -> Optional[ClaudeSession]:
        """Get a session by ID.
        
        Args:
            session_id: The session identifier.
            
        Returns:
            The ClaudeSession if it exists, None otherwise.
        """
        return self.sessions.get(session_id)
        
    async def start(self):
        """Start the session pool cleanup task."""
        if not self._cleanup_task:
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())
            
    async def stop(self):
        """Stop the session pool and clean up all sessions."""
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
                
        # Clean up all sessions
        for session in self.sessions.values():
            await self._cleanup_session(session)
        self.sessions.clear()
        
    async def _cleanup_loop(self):
        """Periodically clean up inactive sessions."""
        while True:
            try:
                await asyncio.sleep(60)  # Check every minute
                current_time = asyncio.get_event_loop().time()
                
                # Find sessions to clean up
                to_remove = []
                for sid, session in self.sessions.items():
                    if not session.is_active and (current_time - session.last_activity) > self.session_timeout:
                        to_remove.append(sid)
                        
                # Clean up expired sessions
                for sid in to_remove:
                    session = self.sessions.pop(sid)
                    await self._cleanup_session(session)
                    log.info("Cleaned up expired session: %s", sid)
                    
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error("Error in cleanup loop: %s", e)
                
    async def _cleanup_session(self, session: ClaudeSession):
        """Clean up a Claude session."""
        # Remove Claude ID mapping if exists
        if session.claude_session_id:
            self.claude_id_map.pop(session.claude_session_id, None)
            
        if session.listener_task:
            session.listener_task.cancel()
            try:
                await session.listener_task
            except asyncio.CancelledError:
                pass
                
        if session.claude:
            try:
                await session.claude.disconnect()
            except:
                pass
                
    async def get_or_create_session(self, session_id: str, resume_id: Optional[str] = None) -> ClaudeSession:
        """Get existing session or create a new one.
        
        Args:
            session_id: The session identifier (iOS tab ID)
            resume_id: Optional Claude SDK session ID to resume from
            
        Returns:
            The Claude session (existing or new)
        """
        # If resuming, look up the tab ID from Claude session ID
        if resume_id and resume_id in self.claude_id_map:
            actual_tab_id = self.claude_id_map[resume_id]
            if actual_tab_id in self.sessions:
                session = self.sessions[actual_tab_id]
                
                # Validate that the tab IDs match or update if needed
                if session_id != actual_tab_id:
                    log.warning("Tab ID mismatch for resume: provided=%s, stored=%s for claude_id=%s", 
                               session_id, actual_tab_id, resume_id)
                    # Update the session's tab ID if iOS is using a different one
                    # This can happen if iOS recreates tabs but wants to resume the same session
                    old_session = self.sessions.pop(actual_tab_id)
                    old_session.session_id = session_id
                    self.sessions[session_id] = old_session
                    session = old_session
                    log.info("Updated tab ID from %s to %s for Claude session %s", 
                            actual_tab_id, session_id, resume_id)
                
                session.last_activity = asyncio.get_event_loop().time()
                session.is_active = True
                log.info("Resuming session: tab_id=%s, claude_id=%s", session_id, resume_id)
                return session
            else:
                log.warning("Claude session ID %s mapped to missing tab %s", resume_id, actual_tab_id)
                # Clean up the orphaned mapping
                del self.claude_id_map[resume_id]
            
        # Check if session already exists with this tab ID
        if session_id in self.sessions:
            session = self.sessions[session_id]
            
            # If we have a resume_id but it doesn't match the stored Claude session ID,
            # we'll need to reconnect with the new ID (handled in _start_claude)
            if resume_id and session.claude_session_id and session.claude_session_id != resume_id:
                log.info("Existing session %s has different Claude ID: stored=%s, requested=%s", 
                        session_id, session.claude_session_id, resume_id)
            
            session.last_activity = asyncio.get_event_loop().time()
            session.is_active = True
            return session
            
        # Create new session
        session = ClaudeSession(session_id=session_id)
        self.sessions[session_id] = session
        log.info("Created new session: %s", session_id)
        return session
        
    def mark_inactive(self, session_id: str):
        """Mark a session as inactive and disconnect Claude SDK (but keep session data for resumption)."""
        if session_id in self.sessions:
            session = self.sessions[session_id]
            session.is_active = False
            session.last_activity = asyncio.get_event_loop().time()
            
            # Disconnect the Claude SDK client but keep the session data
            # This ensures we'll reconnect with resume on next message
            if session.claude:
                try:
                    # Cancel the listener task first
                    if session.listener_task and not session.listener_task.done():
                        session.listener_task.cancel()
                        session.listener_task = None
                    
                    # Just clear the reference - don't try to disconnect in a different task
                    # The Claude SDK will clean up when the process terminates
                    session.claude = None
                    log.info("Cleared Claude SDK reference for inactive session: %s", session_id)
                except Exception as e:
                    log.error("Error clearing Claude SDK for session %s: %s", session_id, e)
            
            log.info("Marked session as inactive: %s", session_id)
            
    def store_claude_session_id(self, tab_id: str, claude_session_id: str):
        """Store the Claude SDK session ID for a tab.
        
        Args:
            tab_id: The iOS tab identifier
            claude_session_id: The Claude SDK's internal session ID
        """
        if tab_id in self.sessions:
            # Remove old mapping if exists
            old_claude_id = self.sessions[tab_id].claude_session_id
            if old_claude_id and old_claude_id in self.claude_id_map:
                del self.claude_id_map[old_claude_id]
                
            # Store new mapping
            self.sessions[tab_id].claude_session_id = claude_session_id
            self.claude_id_map[claude_session_id] = tab_id
            log.info("Stored Claude session ID mapping: %s -> %s", claude_session_id, tab_id)


# Main broker implementation
class KisukeBrokerSDK:
    """WebSocket broker for iOS client to Claude SDK communication.

    Manages a single WebSocket connection for all iOS tabs and routes
    messages to appropriate Claude SDK sessions. Sessions persist across
    WebSocket reconnections.

    Attributes:
        port: WebSocket server port.
        session_pool: Pool of Claude sessions.
        ios_websocket: The single WebSocket connection to iOS.
    """
    def __init__(self, *, port: int = PORT):
        self.port      = port
        self.session_pool = SessionPool()  # Manages all Claude sessions
        self.ios_websocket: Optional[WebSocketServerProtocol] = None  # Single WebSocket
        self._server = None

        self.proxy_host = PROXY_HOST
        self.proxy_port = PROXY_PORT
        self._proxy_runner = None
        self._env_lock = asyncio.Lock()

        self.route_catalog: Dict[str, UpstreamConfig] = {}  # iOS-provided routes (by token)
        self.active_route_token: Optional[str] = None       # which route iOS selected
        self.global_tokens: Dict[str, UpstreamConfig] = {}  # optional mirror for debugging

    async def start(self) -> None:
        """Start the WebSocket server and await connections.

        Binds to all interfaces (IPv4 and IPv6) on the configured port.
        Blocks until server is shut down.
        """
        log.debug("start: Beginning broker startup")
        
        if self._proxy_runner is None:
            log.debug("start: Starting proxy on %s:%d", self.proxy_host, self.proxy_port)
            try:
                self._proxy_runner = await start_proxy(self.proxy_host, self.proxy_port)
                log.info("Embedded Claude proxy listening on http://%s:%d", self.proxy_host, self.proxy_port)
                log.info("WebSocket broker starting on port %d", self.port)
            except Exception as e:
                log.error("Failed to start proxy: %s", e, exc_info=True)
                raise

            # Ensure the stable token exists in the proxy
            if FIXED_ROUTE_TOKEN not in self.global_tokens:
                # Register a placeholder so the proxy knows this token exists
                placeholder = UpstreamConfig()  # empty; will be overwritten when routes arrive
                register_route(FIXED_ROUTE_TOKEN, placeholder)
                self.global_tokens[FIXED_ROUTE_TOKEN] = placeholder

        # Start the session pool cleanup task
        log.debug("start: Starting session pool")
        await self.session_pool.start()

        log.debug("start: Creating WebSocket server on port %d", self.port)
        self._server = await websockets.serve(
            self._handle_ios_client,
            host="",  # Bind to all interfaces for dual-stack IPv4/IPv6
            port=self.port,
        )
        log.info("iOS WebSocket listening on ws://[::1]:%d and ws://127.0.0.1:%d", self.port, self.port)
        log.debug("start: Broker startup complete")

    async def run_forever(self):
        assert self._server is not None
        await self._server.wait_closed()

    async def stop(self):
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _handle_ios_client(self, ws: WebSocketServerProtocol):
        """Handle the iOS client WebSocket connection.

        Manages a single WebSocket for all iOS tabs. Messages are routed
        to appropriate Claude sessions based on session_id in each message.

        Args:
            ws: WebSocket connection from iOS client.
        """
        # Store the single WebSocket connection
        if self.ios_websocket is not None:
            log.warning("Replacing existing iOS WebSocket connection")
            try:
                await self.ios_websocket.close()
            except:
                pass
        
        self.ios_websocket = ws

        # Configure socket for minimal latency
        try:
            ws.transport.get_extra_info("socket").setsockopt(
                socket.IPPROTO_TCP, socket.TCP_NODELAY, 1
            )
        except Exception:
            pass

        # Track if this is a health check connection
        is_health_check = False
        
        # Send initial connection confirmation
        await self._send(ws, {"event": "system", "type": "connected", "session_id": "broker"})
        log.info("iOS connected")
        
        # Reactivate all sessions when iOS reconnects
        reactivated_sessions = []
        for session_id, session in self.session_pool.sessions.items():
            if not session.is_active:
                session.is_active = True
                session.last_activity = asyncio.get_event_loop().time()
                log.info("Reactivated session %s on iOS reconnection", session_id)
                reactivated_sessions.append({
                    "session_id": session_id,
                    "claude_session_id": session.claude_session_id,
                    "is_active": True
                })
        
        # Notify iOS about reactivated sessions
        if reactivated_sessions:
            await self._send(ws, {
                "event": "system",
                "type": "sessions_reactivated", 
                "sessions": reactivated_sessions
            })
            log.info("Notified iOS about %d reactivated sessions", len(reactivated_sessions))

        await self._send(ws, {"event": "system", "type": "request_routes", "session_id": "broker"})

        try:
            async for raw in ws:
                msg = json.loads(raw)
                # Extract session_id from message for routing
                # Only try to extract session_id from payload if it's a dict
                payload = msg.get("payload")
                if isinstance(payload, dict):
                    session_id = msg.get("session_id") or payload.get("session_id") or payload.get("tab_id")
                else:
                    session_id = msg.get("session_id")
                
                # Log differently for health checks vs regular messages
                msg_type = msg.get('type')
                if msg_type == 'health_check':
                    is_health_check = True
                    log.debug("Received health check from iOS")
                else:
                    log.debug(f"Received from iOS: {msg_type} for session {session_id}")
                
                await self._dispatch_ios_msg(msg)
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            # Mark all sessions as inactive but keep them alive for reconnection
            for session_id in list(self.session_pool.sessions.keys()):
                self.session_pool.mark_inactive(session_id)
            
            self.ios_websocket = None
            if is_health_check:
                log.debug("iOS health check completed (disconnected)")
            else:
                log.info("iOS disconnected")

    def _create_model_config(self, config_data: dict, default_model: str = "gpt-4o") -> ModelConfig:
        """Helper to create ModelConfig from configuration data."""
        return ModelConfig(
            provider=config_data.get("provider", "openai"),
            base_url=config_data.get("base_url", "https://api.openai.com/v1"),
            api_key=config_data.get("api_key", ""),
            model=config_data.get("model", default_model),
            extra_headers=config_data.get("extra_headers", {}),
            azure_deployment=config_data.get("azure_deployment"),
            azure_api_version=config_data.get("azure_api_version"),
            auth_method=config_data.get("auth_method"),  # Pass through auth_method from iOS
        )

    async def _register_routes(self, routes: list[dict[str, Any]]):
        added = []
        for r in routes or []:
            token = r.get("token")
            if not token:
                continue

            # Create model configs for each size
            cfg = UpstreamConfig()

            # Map model sizes to their configs with appropriate defaults
            model_sizes = {
                "small": ("small", "gpt-4o-mini"),
                "medium": ("medium", "gpt-4o"),
                "big": ("big", "gpt-4o")
            }

            for attr_name, (key, default_model) in model_sizes.items():
                if key in r and r[key]:
                    setattr(cfg, attr_name, self._create_model_config(r[key], default_model))

            # Store in the catalog only. Don't change the fixed token yet.
            self.route_catalog[token] = cfg
            self.global_tokens[token] = cfg
            added.append(token)

            # First route becomes active by default; copy into the fixed token.
            if not self.active_route_token:
                self.active_route_token = token
            
            # Always re-register the active route with proxy (in case proxy lost it)
            if token == self.active_route_token:
                register_route(FIXED_ROUTE_TOKEN, cfg)  # ensure proxy has current config

        await self._send(self.ios_websocket, {
            "event": "system",
            "type": "routes_registered",
            "tokens": added,
            "active_token": self.active_route_token,
            "stable_token": FIXED_ROUTE_TOKEN
        })



    async def _dispatch_ios_msg(self, msg: Dict[str, Any]):
        """Route incoming iOS messages to appropriate handlers.

        Processes different message types and routes them to the correct
        Claude session based on session_id in the message.

        Args:
            msg: Parsed JSON message from iOS.
        """
        try:
            # Extract session_id for routing
            # Only try to extract session_id from payload if it's a dict
            payload = msg.get("payload")
            if isinstance(payload, dict):
                session_id = msg.get("session_id") or payload.get("session_id") or payload.get("tab_id")
            else:
                session_id = msg.get("session_id")
            
            match msg.get("type"):
                case "health_check":
                    # Explicit health check from iOS app
                    log.debug("Health check received from iOS")
                    await self._send(self.ios_websocket, {
                        "event": "health_check_response",
                        "broker": "kisuke-broker2",
                        "version": "4.0",
                        "active_sessions": len(self.session_pool.sessions),
                        "timestamp": msg.get("payload", {}).get("timestamp", 0)
                    })
                case "ping":
                    # Regular ping - respond with broker status
                    await self._send(self.ios_websocket, {
                        "event": "pong",
                        "broker": "kisuke-broker2",
                        "version": "4.0",
                        "active_sessions": len(self.session_pool.sessions)
                    })
                case "routes":
                    await self._register_routes(msg.get("payload", []))
                case "configure_upstream":
                    await self._register_routes([msg.get("payload", {})])
                case "set_active_route":
                    tok = (msg.get("payload") or {}).get("token")
                    cfg = self.route_catalog.get(tok)
                    if cfg:
                        self.active_route_token = tok
                        # HOT SWITCH: overwrite the proxy's config behind the SINGLE fixed token
                        register_route(FIXED_ROUTE_TOKEN, cfg)

                        # Notify iOS about route change
                        await self._send(self.ios_websocket, {
                            "event": "system",
                            "type": "active_route_changed",
                            "active_token": tok,
                            "stable_token": FIXED_ROUTE_TOKEN
                        })
                    else:
                        await self._send(self.ios_websocket, {
                            "event": "system",
                            "type": "error",
                            "error": "unknown_route_token",
                            "message": f"Token {tok} not registered"
                        })

                case "start":
                    if not session_id:
                        log.error("Start message missing session_id")
                        return
                    # Extract message_id from payload if present
                    payload = msg.get("payload", {})
                    message_id = payload.get("message_id", None) if isinstance(payload, dict) else None
                    log.debug(f"Start event - session_id: {session_id}, message_id: {message_id}")
                    await self._start_claude(session_id, payload, message_id)
                case "send":
                    if not session_id:
                        log.error("Send message missing session_id")
                        return
                    # iOS sends content in payload.content for send messages
                    payload = msg.get("payload", {})
                    content = payload.get("content", "") if isinstance(payload, dict) else ""
                    message_id = payload.get("message_id", None) if isinstance(payload, dict) else None
                    log.debug(f"Send event - session_id: {session_id}, message_id: {message_id}, content: {content!r}")
                    
                    # Don't send ACK here - send it after message is accepted in _relay_user_message
                    await self._relay_user_message(session_id, content, message_id)
                case "interrupt":
                    if session_id:
                        session = self.session_pool.get_session(session_id)
                        if session and session.claude:
                            await session.claude.interrupt()
                case "stop":
                    if session_id:
                        self.session_pool.mark_inactive(session_id)
                        await self._send(self.ios_websocket, {
                            "event": "session_state_change",
                            "type": "subprocess_disconnecting",
                            "session_id": session_id,
                            "chat_id": session_id,
                            "reason": "user_requested"
                        })
                case "permission_response":
                    if session_id:
                        await self._handle_permission_response(session_id, msg.get("payload", {}))
                case "sync_request":
                    # Handle sync request for missed messages
                    if session_id:
                        await self._handle_sync_request(session_id, msg.get("payload", {}))
                case "resend_message":
                    # Handle resend request for unacknowledged messages
                    if session_id:
                        await self._handle_resend_message(session_id, msg.get("payload", {}))
                case _:
                    log.warning("Unknown iOS message: %s", msg)
        except Exception as e:
            log.error("Error dispatching iOS message: %s", e, exc_info=True)
            # Send error back to iOS
            await self._send(self.ios_websocket, {
                "event": "system",
                "type": "error",
                "error": "dispatch_error",
                "message": f"Error handling message: {str(e)}",
                "session_id": msg.get("session_id", "unknown")
            })

    async def _start_claude(self, session_id: str, cfg: Dict[str, Any], message_id: Optional[str] = None):
        """Initialize Claude SDK client for a session.

        Creates and connects a Claude SDK client with the specified
        configuration. Handles both new sessions and resumption.

        Args:
            session_id: The session identifier (iOS tab ID).
            cfg: Configuration dictionary with options like permission_mode,
                workdir, system_prompt, etc.
            message_id: Optional message ID for ACK protocol.
        """
        # Get or create session (handles resume logic)
        resume_id = cfg.get("resume")
        session = await self.session_pool.get_or_create_session(session_id, resume_id)
        
        # Check if we need to reconnect with a different session ID
        resume_id_to_use = cfg.get("resume")
        needs_reconnect = False
        
        if session.claude and session.is_active:
            # Check if we're trying to resume with a different Claude session ID
            if resume_id_to_use and session.claude_session_id != resume_id_to_use:
                log.info("Session ID mismatch - need to reconnect. Current: %s, Requested: %s", 
                        session.claude_session_id, resume_id_to_use)
                needs_reconnect = True
            # Check if Claude SDK is actually connected
            elif session.claude and not hasattr(session.claude, '_transport') or not session.claude._transport:
                log.info("Claude SDK not connected - need to reconnect")
                needs_reconnect = True
            else:
                # Same session and connected - just send the prompt
                prompt = cfg.get("prompt", "").strip()
                if prompt:
                    await self._relay_user_message(session_id, prompt, message_id)
                else:
                    # Send ACK for successful session check even without prompt
                    if message_id and self.ios_websocket:
                        await self._send(self.ios_websocket, {
                            "event": "message_ack",
                            "session_id": session_id,
                            "message_id": message_id,
                            "broker_message_id": f"broker_{message_id}",
                            "timestamp": asyncio.get_event_loop().time()
                        })
                        log.debug(f"Sent ACK for start message (session already active): {message_id}")
                return
        
        # If we need to reconnect, clean up the old client
        if needs_reconnect and session.claude:
            try:
                await session.claude.disconnect()
            except:
                pass
            session.claude = None

        try:
            log.info("Starting Claude for session %s (resume: %s)", session_id, resume_id)
            log.info("   Config: %r", cfg)
            
            # Clean up any existing Claude instance
            if session.claude:
                try:
                    await session.claude.disconnect()
                except:
                    pass
                session.claude = None

            permission_mode = cfg.get("permission_mode", "bypassPermissions")
            log.debug("Starting with permission_mode: %s", permission_mode)
            
            session.permission_mode = permission_mode

            # Initialize plan mode tracking
            if permission_mode == "plan":
                session.in_plan_mode = True
                # Store the original mode (from the UI) if not already stored
                if not session.original_permission_mode:
                    # Store the mode to return to after plan completion
                    session.original_permission_mode = cfg.get("original_permission_mode") or "default"
                    log.info("Entering plan mode, will return to: %s", session.original_permission_mode)

                    # Emit session state change event
                    await self._send(self.ios_websocket, {
                        "event": "session_state_change",
                        "type": "permission_mode_change",
                        "from": session.original_permission_mode,
                        "to": "plan",
                        "reason": "user_initiated",
                        "session_id": session_id,
                        "chat_id": session_id
                    })

            # Validate and expand the working directory path
            workdir = cfg.get("workdir")
            if workdir:
                exists, expanded_workdir = PathUtils.ensure_directory_exists(workdir)
                log.debug("Workdir check: raw=%r expanded=%r exists=%s", workdir, expanded_workdir, exists)
                try:
                    log.debug("os.path.isdir(%r)=%s", expanded_workdir, os.path.isdir(expanded_workdir))
                    log.debug("os.path.exists(%r)=%s", expanded_workdir, os.path.exists(expanded_workdir))
                    log.debug("os.access(%r, R_OK|X_OK)=%s", expanded_workdir,
                              os.access(expanded_workdir, os.R_OK | os.X_OK))
                    log.debug("Effective UID=%s, GID=%s", os.geteuid(), os.getegid())
                except Exception as e:
                    log.error("Error while probing workdir %r: %s", expanded_workdir, e)

                if not exists:
                    raise ValueError(
                        f"Working directory does not exist: {workdir} "
                        f"(expanded={expanded_workdir})"
                    )
                log.info("Using working directory: %s (expanded from: %s)", expanded_workdir, workdir)
                workdir = expanded_workdir
            else:
                workdir = os.getcwd()
                log.info("Using current directory: %s", workdir)
            
            session.workdir = workdir

            # Determine the Claude SDK session ID for resume
            resume_claude_id = None
            
            # Priority order for resume ID:
            # 1. If resume_id matches a known Claude session ID, use it
            # 2. If session has a stored Claude session ID and no conflicting resume_id, use stored
            # 3. If resume_id provided but no match found, use it (might be a new Claude session)
            
            if resume_id:
                # Check if this resume_id is a known Claude session ID
                if resume_id in self.session_pool.claude_id_map:
                    resume_claude_id = resume_id
                    log.info("Resume ID is a known Claude session ID: %s", resume_claude_id)
                # Check if session already has a Claude session ID
                elif session.claude_session_id:
                    if session.claude_session_id == resume_id:
                        resume_claude_id = resume_id
                        log.info("Resume ID matches stored Claude session ID: %s", resume_claude_id)
                    else:
                        # Conflict - iOS wants to resume different session
                        # Use the requested resume_id (will trigger reconnect above)
                        resume_claude_id = resume_id
                        log.warning("Resume ID conflicts with stored: requested=%s, stored=%s. Using requested.", 
                                  resume_id, session.claude_session_id)
                else:
                    # No stored Claude session ID, use the provided resume_id
                    resume_claude_id = resume_id
                    log.info("Using provided resume ID (no stored session): %s", resume_claude_id)
            elif session.claude_session_id:
                # No resume_id provided but session has a stored Claude session ID
                resume_claude_id = session.claude_session_id
                log.info("No resume ID provided, using stored Claude session ID: %s", resume_claude_id)
            else:
                log.info("No resume ID available - will create new Claude session")
            
            options = ClaudeCodeOptions(
                permission_mode = permission_mode,
                allowed_tools   = cfg.get("allowed_tools", []),
                cwd             = workdir,
                system_prompt   = cfg.get("system_prompt"),
                permission_prompt_tool_name = "stdio",   # Enable control channel for permissions
                resume = resume_claude_id,  # Pass Claude SDK session ID for resumption
                continue_conversation = bool(resume_claude_id),  # Required for resume to work
            )

            async def _perm_handler(tool, inp, req_id):
                return await self._permission_flow(session, tool, inp, req_id)

            log.debug("Creating PermAwareClient with options: %r", options)
            session.claude = PermAwareClient(
                options=options, 
                permission_handler=_perm_handler, 
                session=session,
                broker=self  # Pass broker reference for session pool access
            )

            # Always use the fixed token for ALL tabs
            session_env = {
                "ANTHROPIC_BASE_URL": f"http://{self.proxy_host}:{self.proxy_port}",
                "ANTHROPIC_API_KEY": FIXED_ROUTE_TOKEN,  # always this single token
            }

            log.debug("Connecting to Claude SDK via proxy %s:%d (token=%s)",
                          self.proxy_host, self.proxy_port, FIXED_ROUTE_TOKEN)

            async with self._env_lock:
                prev = {k: os.environ.get(k) for k in session_env}
                os.environ.update(session_env)
                try:
                    await session.claude.connect()
                finally:
                    # restore env
                    for k, v in prev.items():
                        if v is None: os.environ.pop(k, None)
                        else: os.environ[k] = v

            log.debug("Claude SDK connected successfully")

            # Don't send subprocess_connected here - wait for Claude SDK to send init with actual session ID
            # The init message from Claude SDK will contain the real session ID for resume functionality

            log.debug("Creating listener task...")
            session.listener_task = asyncio.create_task(self._claude_listener(session))

            log.info("Claude subprocess ready - %s", session_id)

            # Send init success event to iOS
            await self._send(self.ios_websocket, {
                "event": "system",
                "type": "claude_ready",
                "session_id": session_id
            })

            initial_prompt = (cfg.get("prompt") or "").strip()
            if initial_prompt:
                log.info("Sending initial prompt (%d chars) for session %s",
                         len(initial_prompt), session_id)
                # Pass message_id to _relay_user_message which will send ACK
                await self._relay_user_message(session_id, initial_prompt, message_id)
            else:
                log.debug("No initial prompt provided in start payload.")
                # Send ACK for successful session start even without prompt
                if message_id and self.ios_websocket:
                    await self._send(self.ios_websocket, {
                        "event": "message_ack",
                        "session_id": session_id,
                        "message_id": message_id,
                        "broker_message_id": f"broker_{message_id}",
                        "timestamp": asyncio.get_event_loop().time()
                    })
                    log.debug(f"Sent ACK for start message: {message_id}")

        except Exception as e:
            log.error("Failed to start Claude client: %s", e, exc_info=True)

            # Clean up partial initialization
            if session.claude:
                try:
                    await session.claude.disconnect()
                except:
                    pass
                session.claude = None

            # Send error event to iOS instead of dropping connection
            await self._send(self.ios_websocket, {
                "event": "system",
                "type": "error",
                "error": "claude_init_failed",
                "message": f"Failed to initialize Claude: {str(e)}",
                "session_id": session_id
            })

    # ------------------------------------------------------------ user → Claude
    async def _relay_user_message(self, session_id: str, text: str, message_id: Optional[str] = None):
        """Send a user message to Claude."""
        session = self.session_pool.get_session(session_id)
        if not session:
            log.error("No session found for %s", session_id)
            return
        
        # Check for duplicate message ID
        if message_id and message_id in session.pending_messages:
            existing = session.pending_messages[message_id]
            if existing.get("status") == "acknowledged":
                log.info("Duplicate message %s already acknowledged, skipping", message_id)
                # Re-send ACK in case iOS didn't receive it
                if self.ios_websocket:
                    await self._send(self.ios_websocket, {
                        "event": "message_ack",
                        "session_id": session_id,
                        "message_id": message_id,
                        "broker_message_id": f"broker_{message_id}",
                        "timestamp": asyncio.get_event_loop().time()
                    })
                return
            elif existing.get("status") == "sending":
                log.info("Message %s already being processed, skipping duplicate", message_id)
                return
            elif existing.get("status") == "failed":
                # Allow retry of failed messages
                log.info("Retrying failed message %s", message_id)
                # Reset status to allow retry
                session.pending_messages[message_id]["status"] = "sending"
                session.pending_messages[message_id]["timestamp"] = asyncio.get_event_loop().time()
                # Continue processing below
        
        # Track message ID if provided (only if not already tracked from retry)
        if message_id and message_id not in session.pending_messages:
            session.last_user_message_id = message_id
            session.pending_messages[message_id] = {
                "content": text,
                "timestamp": asyncio.get_event_loop().time(),
                "status": "sending"
            }
            
        # Check if we need to reconnect the Claude SDK session
        needs_reconnect = False
        if not session.claude:
            log.info("No Claude SDK connection for session %s, need to reconnect", session_id)
            needs_reconnect = True
        elif not session.is_active:
            # Session was marked inactive (e.g., after iOS disconnection)
            # The Claude SDK connection is definitely stale since we disconnect it in mark_inactive
            log.info("Session %s was inactive, need to reconnect", session_id)
            needs_reconnect = True
        else:
            # Even if session is active, check if the transport is still alive
            try:
                # Try to check if the connection is still valid
                if hasattr(session.claude, '_transport') and hasattr(session.claude._transport, '_process'):
                    if session.claude._transport._process.returncode is not None:
                        log.info("Claude SDK process has terminated for session %s, need to reconnect", session_id)
                        needs_reconnect = True
            except Exception as e:
                log.debug("Error checking Claude SDK health: %s", e)
                needs_reconnect = True
            
        if needs_reconnect:
            # Disconnect any existing connection cleanly
            if session.claude:
                try:
                    if session.listener_task and not session.listener_task.done():
                        session.listener_task.cancel()
                        session.listener_task = None
                except Exception as e:
                    log.debug("Error canceling listener task: %s", e)
                session.claude = None
            
            # If we have a stored Claude session ID, try to resume it
            # But be prepared for it to fail if the session expired
            if session.claude_session_id:
                log.info("Attempting to resume Claude session: %s", session.claude_session_id)
                
                # Create config for resumption with correct workdir
                # DO NOT include the message here - we'll send it after resume
                resume_config = {
                    "permission_mode": "default",
                    "workdir": session.workdir or os.getcwd(),
                    "resume": session.claude_session_id,
                    "session_id": session_id,
                    "tab_id": session_id
                    # NO prompt here - resume should restore previous state only
                }
                
                # Try to resume the session
                try:
                    await self._start_claude(session_id, resume_config)
                    
                    # Re-fetch the session after reconnection
                    session = self.session_pool.get_session(session_id)
                    if session and session.claude:
                        log.info("Successfully resumed Claude session %s", session.claude_session_id)
                        
                        # Now send the message through the normal flow
                        # Don't return - continue to normal message sending below
                        needs_reconnect = False
                    else:
                        log.warning("Resume appeared to succeed but no Claude connection")
                        needs_reconnect = True
                except Exception as e:
                    log.warning("Failed to resume Claude session %s: %s", session.claude_session_id, e)
                    # Fall through to create new session
                    needs_reconnect = True
            
            # If resume failed or no session to resume, still need to reconnect
            if needs_reconnect:
                # Create a new session WITHOUT the message initially
                log.info("Creating new Claude session for %s", session_id)
                new_config = {
                    "permission_mode": "default",
                    "workdir": session.workdir or os.getcwd(),
                    "session_id": session_id,
                    "tab_id": session_id
                    # NO prompt here - we'll send message after session is ready
                }
                
                try:
                    await self._start_claude(session_id, new_config)
                    
                    # Re-fetch the session after creation
                    session = self.session_pool.get_session(session_id)
                    if not session or not session.claude:
                        log.error("Failed to create Claude session")
                        raise Exception("Session creation failed - no Claude connection")
                except Exception as e:
                    log.error("Failed to create new Claude session: %s", e)
                    
                    # Mark message as failed if we have tracking
                    if message_id:
                        session = self.session_pool.get_session(session_id)
                        if session and message_id in session.pending_messages:
                            session.pending_messages[message_id]["status"] = "failed"
                            session.pending_messages[message_id]["error"] = str(e)
                    
                    await self._send(self.ios_websocket, {
                        "event": "system",
                        "type": "error",
                        "error": "session_creation_failed",
                        "message": f"Failed to create Claude session: {str(e)}",
                        "session_id": session_id
                    })
                    return
        
        # If we have an active connection, send the message normally
        session.is_active = True
        session.last_activity = asyncio.get_event_loop().time()

        async def prompt_stream() -> AsyncIterator[Dict[str, Any]]:
            yield {
                "type":  "user",
                "message": {"role": "user", "content": text},
                "parent_tool_use_id": None,
                "session_id": session_id,
            }

        try:
            # Send message to Claude
            await session.claude.query(prompt_stream(), session_id=session_id)
            
            # NOW send ACK after Claude accepted the message
            if message_id and self.ios_websocket:
                await self._send(self.ios_websocket, {
                    "event": "message_ack",
                    "session_id": session_id,
                    "message_id": message_id,
                    "broker_message_id": f"broker_{message_id}",
                    "timestamp": asyncio.get_event_loop().time()
                })
                log.debug(f"Sent ACK for message: {message_id}")
                
                # Update message status in session tracking
                if message_id in session.pending_messages:
                    session.pending_messages[message_id]["status"] = "acknowledged"
                    
                    # Clean up old acknowledged messages (older than 5 minutes)
                    cutoff_time = asyncio.get_event_loop().time() - 300
                    to_remove = [mid for mid, msg in session.pending_messages.items() 
                                if msg.get("status") == "acknowledged" and 
                                msg.get("timestamp", 0) < cutoff_time]
                    for mid in to_remove:
                        del session.pending_messages[mid]
                        log.debug(f"Cleaned up old message: {mid}")
                    
        except Exception as e:
            log.error("Failed to send message to Claude: %s", e)
            # Mark session as needing reconnection for next message
            session.is_active = False
            
            # If we have a message_id, mark it as failed
            if message_id and message_id in session.pending_messages:
                session.pending_messages[message_id]["status"] = "failed"
                session.pending_messages[message_id]["error"] = str(e)

    # ------------------------------------------------------- Claude → broker loop
    async def _claude_listener(self, session: ClaudeSession):
        """Listen for messages from Claude and forward to iOS."""
        assert session.claude is not None

        try:
            async for msg in session.claude.receive_messages():
                # Log the message type for debugging
                log.debug("📨 _claude_listener received message type: %s", type(msg).__name__)

                # Normal message handling
                await self._emit_to_ios(session, msg)
        except Exception as e:
            log.error("Claude listener error for session %s: %s", session.session_id, e)

    async def _emit_to_ios(self, session: ClaudeSession, m):
        if isinstance(m, AssistantMessage):
            blocks: list[Dict[str, Any]] = []
            for b in m.content:
                if isinstance(b, TextBlock):
                    blocks.append({"type": "text", "text": b.text})
                elif isinstance(b, ToolUseBlock):
                    log.debug("🔧 Emitting ToolUseBlock to iOS: %s (id=%s)", b.name, b.id)
                    tool_use_data = {
                        "type": "tool_use",
                        "id": b.id,
                        "name": b.name,
                        "input": b.input
                    }
                    # Add any additional metadata from the block
                    if hasattr(b, 'cache_control'):
                        tool_use_data["cache_control"] = b.cache_control
                    blocks.append(tool_use_data)

                    # Track this tool use for later result matching
                    session.pending_tools[b.id] = {
                        "name": b.name,
                        "input": b.input,
                        "timestamp": asyncio.get_event_loop().time()
                    }
                elif isinstance(b, ToolResultBlock):
                    blocks.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": b.tool_use_id,
                            "content": b.content,
                            "is_error": b.is_error,
                        }
                    )

            # Include all message fields including usage data
            message_data = {
                "event": "stream",
                "data": blocks,
                **session.ctx()
            }
            
            # Include correlation with user message if available
            if session.last_user_message_id:
                message_data["user_message_id"] = session.last_user_message_id

            # Add message metadata if available
            message_info = {}
            for attr in ['id', 'type', 'role', 'model', 'stop_reason', 'stop_sequence', 'usage']:
                if hasattr(m, attr):
                    message_info[attr] = getattr(m, attr)

            if message_info:
                message_data["message"] = message_info

            await self._send(self.ios_websocket, message_data)

        elif isinstance(m, UserMessage):
            # Handle tool results that come back as UserMessages
            log.debug("🎯 _emit_to_ios handling UserMessage with %d content items", len(m.content))
            tool_results: list[Dict[str, Any]] = []

            for item in m.content:
                if isinstance(item, ToolResultBlock):
                    log.debug("📊 Tool result received: tool_use_id=%s, is_error=%s",
                            item.tool_use_id, item.is_error)

                    # Get tool info from pending tools
                    tool_info = session.pending_tools.get(item.tool_use_id, {})
                    tool_name = tool_info.get("name", "unknown")

                    # Send individual tool result event
                    tool_result_event = {
                        "event": "tool_result",
                        "tool_use_id": item.tool_use_id,
                        "tool_name": tool_name,
                        "content": item.content,
                        "is_error": item.is_error,
                        **session.ctx()
                    }

                    # Add error details if available
                    if item.is_error:
                        tool_result_event["error_type"] = "tool_execution_error"
                        log.info("❌ Tool error for %s: %s", tool_name, item.content)
                    else:
                        log.info("✅ Tool success for %s", tool_name)

                    await self._send(self.ios_websocket, tool_result_event)

                    # Also emit tool completion event
                    await self._send(self.ios_websocket, {
                        "event": "tool_execution",
                        "type": "complete",
                        "tool_use_id": item.tool_use_id,
                        "tool_name": tool_name,
                        "success": not item.is_error,
                        "error": item.content if item.is_error else None,
                        **session.ctx()
                    })

                    # Clean up pending tool
                    session.pending_tools.pop(item.tool_use_id, None)

                    # Also collect for batch event
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": item.tool_use_id,
                        "content": item.content,
                        "is_error": item.is_error
                    })
                elif isinstance(item, TextBlock):
                    # Some user messages might contain text
                    tool_results.append({"type": "text", "text": item.text})

            # If we have tool results, also send a stream event for consistency
            if tool_results:
                await self._send(self.ios_websocket, {
                    "event": "stream",
                    "data": tool_results,
                    "message_type": "user",
                    **session.ctx()
                })

        elif isinstance(m, ResultMessage):
            u = m.usage or {}
            # Keep per-kind totals for UI
            for k in list(session.last_usage.keys()):
                session.last_usage[k] = int(u.get(k, session.last_usage.get(k, 0)))

            # Keep aggregate for backward compatibility
            session.used_tokens = session.last_usage["input_tokens"] + session.last_usage["output_tokens"]

            # Include all result fields
            result_data = {
                "event": "result",
                "success": not m.is_error,
                "is_error": m.is_error,
                "total_cost_usd": m.total_cost_usd,
                "subtype": m.subtype,
                "duration_ms": getattr(m, 'duration_ms', None),
                "duration_api_ms": getattr(m, 'duration_api_ms', None),
                "num_turns": getattr(m, 'num_turns', None),
                "result": getattr(m, 'result', None),
                "usage": u,  # Include raw usage object
                **session.ctx(),
            }
            await self._send(self.ios_websocket, result_data)

        elif isinstance(m, SystemMessage):
            # Include all system message fields
            system_data = {
                "event": "system",
                "subtype": m.subtype,
                "chat_id": session.session_id,  # Add chat_id for iOS routing
            }

            # For init messages, copy ALL attributes
            if m.subtype == "init":
                # Get all non-private attributes
                for attr in dir(m):
                    if not attr.startswith('_') and attr not in ['subtype'] and hasattr(m, attr):
                        value = getattr(m, attr)
                        # Skip methods
                        if not callable(value):
                            system_data[attr] = value

                # Get Claude's session ID from the session pool (already stored by PermAwareClient)
                claude_session_id = session.claude_session_id
                if not claude_session_id and hasattr(session.claude, '_init_session_id'):
                    # Fallback: get from client if not in session yet
                    claude_session_id = session.claude._init_session_id
                    # Store it properly
                    self.session_pool.store_claude_session_id(session.session_id, claude_session_id)
                    
                if claude_session_id:
                    # IMPORTANT: Send BOTH IDs to iOS - Claude SDK ID and tab ID
                    system_data['sessionId'] = claude_session_id  # Claude SDK session ID for resume
                    system_data['tabId'] = session.session_id     # iOS tab ID for routing
                    log.info("✅ Sending both IDs to iOS - Claude: %s, Tab: %s", 
                            claude_session_id, session.session_id)
                else:
                    log.warning("⚠️ No Claude session ID available for init event")
                    # Still send tab ID for routing
                    system_data['tabId'] = session.session_id
            else:
                # For non-init messages, copy specific fields
                for attr in ['cwd', 'session_id', 'tools', 'mcp_servers', 'model', 'permissionMode', 'apiKeySource']:
                    if hasattr(m, attr):
                        system_data[attr] = getattr(m, attr)

            await self._send(self.ios_websocket, system_data)

    async def _permission_flow(
        self, session: ClaudeSession, tool_name: str, tool_input: Dict[str, Any], req_id: str
    ) -> Dict[str, Any]:
        """Handle tool permission request flow.

        Checks cached permissions first, then forwards to iOS client if needed.
        Tracks permission state and emits lifecycle events.

        Args:
            session: Claude session requesting permission.
            tool_name: Name of the tool requesting permission.
            tool_input: Input parameters for the tool.
            req_id: Unique request identifier.

        Returns:
            Permission response dictionary with behavior and optional message.
        """
        log.debug("_permission_flow called for tool=%s, req_id=%s", tool_name, req_id)
        log.debug("   Session allowed_tools: %s", session.allowed_tools)
        log.debug("   Session denied_tools: %s", session.denied_tools)

        # immediate answers from cached choices
        if tool_name in session.allowed_tools:
            log.debug("   Tool '%s' is pre-allowed, returning immediate allow", tool_name)
            return {"behavior": "allow"}
        if tool_name in session.denied_tools:
            log.debug("   Tool '%s' is pre-denied, returning immediate deny", tool_name)
            return {"behavior": "deny", "message": f"{tool_name} was previously denied"}

        fut: asyncio.Future = asyncio.Future()
        session.pending_permissions[req_id] = fut

        await self._send(
            self.ios_websocket,
            {
                "event": "permission_request",
                "chat_id": session.session_id,  # Use session_id for iOS routing
                "request_id": req_id,
                "tool_name": tool_name,
                "input": tool_input,
            },
        )
        log.info("Asking iOS for %s (%s)", tool_name, req_id)

        try:
            resp: Dict[str, Any] = await fut          # waits for _handle_permission_response

            # Emit tool execution lifecycle event
            if resp.get("behavior") == "allow":
                await self._send(self.ios_websocket, {
                    "event": "tool_execution",
                    "type": "start",
                    "tool_name": tool_name,
                    "request_id": req_id,
                    "session_id": session.session_id,
                    "chat_id": session.session_id
                })
            elif resp.get("behavior") == "deny":
                await self._send(self.ios_websocket, {
                    "event": "tool_execution",
                    "type": "denied",
                    "tool_name": tool_name,
                    "request_id": req_id,
                    "reason": resp.get("message", f"User denied {tool_name}"),
                    "session_id": session.session_id,
                    "chat_id": session.session_id
                })

            return resp
        finally:
            session.pending_permissions.pop(req_id, None)

    async def _handle_permission_response(self, session_id: str, payload: Dict[str, Any]):
        """Process permission response from iOS client.

        Handles the iOS client's response to a permission request, including
        special handling for ExitPlanMode and permanent permission settings.

        Args:
            session_id: ID of the session that received the response.
            payload: Response data including behavior, tool_name, and flags.
        """
        # Get the session
        session = self.session_pool.get_session(session_id)
        if not session:
            log.error("No session found for session_id: %s", session_id)
            return
        
        req_id   = payload.get("request_id")
        behavior = payload.get("behavior")            # "allow" | "deny"
        tool     = payload.get("tool_name")
        always   = payload.get("perm_always", False)

        fut = session.pending_permissions.get(req_id)
        if fut is None:
            log.warning("Unknown permission request id %s", req_id)
            return

        # Special handling for ExitPlanMode approval
        if tool == "ExitPlanMode":
            if behavior == "allow":
                log.info("ExitPlanMode approved for session %s", session.session_id)

                # Determine target mode based on 'always' flag
                target_mode = "acceptEdits" if always else "default"
                log.info("Plan approved! Will exit plan mode and resume with %s permissions", target_mode)

                # Get current session info
                claude_session_id = getattr(session.claude, '_init_session_id', None) if session.claude else None
                cwd = session.claude.options.cwd if session.claude and session.claude.options else None

                if not claude_session_id:
                    log.error("Cannot exit plan mode: no Claude session ID available")
                    fut.set_result({"behavior": "deny", "message": "No session ID available for plan mode exit"})
                    return

                log.info("Immediately transitioning from plan mode to %s", target_mode)
                log.info("   Claude session ID: %s", claude_session_id)
                log.info("   Working directory: %s", cwd)

                # Create the response for Claude
                resp = {
                    "behavior": behavior,
                    "perm_always": always,
                    "message": payload.get("message"),
                }

                # First, resolve the permission future so Claude gets the response
                fut.set_result(resp)
                session.pending_permissions.pop(req_id, None)

                # Reset plan mode state
                session.in_plan_mode = False

                # Shutdown current Claude subprocess
                await self._shutdown_session(session_id, keep_session=True)

                # Small delay to ensure clean shutdown
                await asyncio.sleep(0.5)

                # Restart with new permission mode
                resume_config = {
                    "permission_mode": target_mode,
                    "resume": claude_session_id,
                    "workdir": cwd,
                }

                log.info("Restarting Claude with permission_mode=%s", target_mode)
                await self._start_claude(session_id, resume_config)

                # Send mode_changed event to iOS
                await self._send(self.ios_websocket, {
                    "event": "system",
                    "type": "mode_changed",
                    "from": "plan",
                    "to": target_mode,
                    "session_id": session.session_id,
                    "chat_id": session.session_id
                })

                return  # Don't process the response again below
            else:
                # Plan rejected - interrupt Claude execution
                log.info("Plan denied! Interrupting...")
                if session.claude:
                    asyncio.create_task(session.claude.interrupt())

        # remember long-term choices
        if always:
            (session.allowed_tools if behavior == "allow" else session.denied_tools).add(tool)

        # notify UI for optimistic updates
        await self._send(
            self.ios_websocket,
            {
                "event": "system",
                "type":  "permissions_updated",
                "update": {"action": behavior, "tool_name": tool, "perm_always": always},
            },
        )

        # Build the response object
        result: Dict[str, Any] = {"behavior": behavior}
        if "updatedInput" in payload:
            result["updatedInput"] = payload["updatedInput"]
        if "message" in payload:
            result["message"] = payload["message"]
        elif behavior == "deny":
            result["message"] = f"User denied {tool}"

        fut.set_result(result)
        log.info("%s -> %s  (always=%s)", tool, behavior, always)

    async def _handle_sync_request(self, session_id: str, payload: Dict[str, Any]):
        """Handle sync request for missed messages from iOS client.
        
        Args:
            session_id: ID of the session requesting sync.
            payload: Sync request data including last known message IDs.
        """
        log.info("Sync request for session %s: %s", session_id, payload)
        
        # Send sync response to iOS
        await self._send(self.ios_websocket, {
            "event": "sync_response",
            "session_id": session_id,
            "last_sent_message_id": payload.get("last_sent_message_id"),
            "last_received_message_id": payload.get("last_received_message_id"),
            "status": "synced",
            "timestamp": asyncio.get_event_loop().time()
        })
        
        # If there are pending messages to resend, iOS will request them individually
        log.debug("Sent sync response for session %s", session_id)

    async def _handle_resend_message(self, session_id: str, payload: Dict[str, Any]):
        """Handle resend request for unacknowledged messages from iOS client.
        
        Args:
            session_id: ID of the session requesting resend.
            payload: Resend request data including message ID.
        """
        message_id = payload.get("message_id")
        content = payload.get("content", "")
        
        log.info("Resend request for session %s, message %s", session_id, message_id)
        
        # Send ACK for the resent message
        if message_id:
            await self._send(self.ios_websocket, {
                "event": "message_ack",
                "session_id": session_id,
                "message_id": message_id,
                "broker_message_id": f"broker_resend_{message_id}",
                "timestamp": asyncio.get_event_loop().time()
            })
            log.debug(f"Sent ACK for resent message: {message_id}")
        
        # Relay the message to Claude
        if content:
            await self._relay_user_message(session_id, content)

    async def _send(self, ws: WebSocketServerProtocol, obj: Dict[str, Any]):
        """Send JSON message to WebSocket client.

        Args:
            ws: WebSocket connection.
            obj: Dictionary to send as JSON.
        """
        try:
            await ws.send(json.dumps(obj))
        except Exception as exc:
            log.debug("Send failed: %s", exc)

    async def _shutdown_session(self, session_id: str, keep_session: bool = False):
        """Clean up session resources.

        Cancels listener task, disconnects Claude client, and optionally
        removes session from active sessions.

        Args:
            session_id: ID of the session to shut down.
            keep_session: If True, preserve session for resumption.
        """
        # Get the session
        session = self.session_pool.get_session(session_id)
        if not session:
            log.warning("No session found to shutdown: %s", session_id)
            return
        
        # Emit session state change event before shutdown
        if session.claude:
            await self._send(self.ios_websocket, {
                "event": "session_state_change",
                "type": "subprocess_disconnecting",
                "session_id": session.session_id,
                "reason": "resuming" if keep_session else "shutdown",
                "chat_id": session.session_id
            })

        if session.listener_task and not session.listener_task.done():
            session.listener_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await session.listener_task
        if session.claude:
            await session.claude.disconnect()
            session.claude = None
        session.listener_task = None

        # Only remove from sessions if not keeping for resumption
        if not keep_session:
            self.session_pool.sessions.pop(session_id, None)




async def _main():
    """Main entry point for the broker.

    Sets up signal handlers and starts the WebSocket server.
    """
    log.debug("_main: Creating broker instance")
    broker = KisukeBrokerSDK()
    stop = asyncio.Event()

    log.debug("_main: Setting up signal handlers")
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    # start both WS server and embedded proxy
    log.debug("_main: Creating server task")
    server_task = asyncio.create_task(broker.start())
    
    log.debug("_main: Waiting for stop signal")
    await stop.wait()
    await broker.stop()

    server_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await server_task

if __name__ == "__main__":
    try:
        import uvloop
        asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
    except ImportError:
        pass

    asyncio.run(_main())
