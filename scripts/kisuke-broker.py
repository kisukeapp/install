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
        session = None,                    # Reference to parent Session object
        options: ClaudeCodeOptions | None = None,
    ):
        super().__init__(options=options)
        self._perm_handler = permission_handler
        self._session = session            # Reference to broker Session
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
                        if self._session and hasattr(self._session, 'ios_ws'):
                            try:
                                broker = self._session.ios_ws.ws_server.ws_handler if hasattr(self._session.ios_ws, 'ws_server') else None
                                if not broker:
                                    # Try to get broker from session
                                    for attr_name in dir(self._session):
                                        attr = getattr(self._session, attr_name)
                                        if hasattr(attr, '_send'):
                                            broker = attr
                                            break

                                # Send tool result event
                                await self._session.ios_ws.send(json.dumps({
                                    "event": "tool_result",
                                    "tool_use_id": item.get("tool_use_id"),
                                    "content": item.get("content"),
                                    "is_error": item.get("is_error", False),
                                    "session_id": self._session.session_id,
                                    "chat_id": self._session.chat_id
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

                    # Also update the session if we have access to it
                    if self._session:
                        self._session.claude_session_id = self._init_session_id
                        log.info("Updated session.claude_session_id: %s", self._init_session_id)

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


# Session management for iOS client connections
@dataclass
class Session:
    """Manages state for a single iOS client connection.

    Each iOS tab gets its own Session that tracks the WebSocket connection,
    Claude subprocess, permission state, and message routing.

    Attributes:
        session_id: Unique identifier for this broker session.
        ios_ws: WebSocket connection to the iOS client.
        chat_id: iOS chat tab identifier for routing."""

    session_id: str                                       # Internal broker session ID
    ios_ws: WebSocketServerProtocol                       # WebSocket to iOS client
    chat_id: Optional[str] = None                        # iOS chat tab ID

    claude: Optional[PermAwareClient] = None              # Claude SDK client instance
    listener_task: Optional[asyncio.Task] = None          # Task for receiving Claude messages

    allowed_tools: Set[str] = field(default_factory=set)  # Tools with permanent allow
    denied_tools:  Set[str] = field(default_factory=set)  # Tools with permanent deny

    used_tokens: int = 0                                  # Running token count
    pending_permissions: Dict[str, asyncio.Future] = field(default_factory=dict)  # Active permission requests
    pending_tools: Dict[str, Dict[str, Any]] = field(default_factory=dict)        # Active tool executions

    # Plan mode state management
    claude_session_id: Optional[str] = None               # Claude's internal session ID
    original_permission_mode: Optional[str] = None        # Mode to return to after plan
    in_plan_mode: bool = False                           # Currently in plan mode

    def ctx(self) -> Dict[str, Any]:
        """Generate context payload for outbound messages.

        Returns:
            Dictionary with session context including tokens and IDs.
        """
        return {
            "session_id":   self.session_id,
            "chat_id":      self.chat_id,  # Add chat_id for iOS routing
            "used_tokens":  self.used_tokens,
            "context_left": CONTEXT_LIMIT - self.used_tokens,
        }


# Main broker implementation
class KisukeBrokerSDK:
    """WebSocket broker for iOS client to Claude SDK communication.

    Manages WebSocket server, client sessions, and message routing between
    iOS clients and Claude SDK subprocesses. Handles multiple concurrent
    sessions with independent permission states.

    Attributes:
        port: WebSocket server port.
        sessions: Active sessions indexed by session ID.
    """
    def __init__(self, *, port: int = PORT):
        self.port      = port
        self.sessions: Dict[str, Session] = {}
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

        if self._proxy_runner is None:
            self._proxy_runner = await start_proxy(self.proxy_host, self.proxy_port)
            log.info("Embedded Claude proxy listening on http://%s:%d", self.proxy_host, self.proxy_port)
            log.info("WebSocket broker starting on port %d", self.port)

            # Ensure the stable token exists in the proxy
            if FIXED_ROUTE_TOKEN not in self.global_tokens:
                # Register a placeholder so the proxy knows this token exists
                placeholder = UpstreamConfig()  # empty; will be overwritten when routes arrive
                register_route(FIXED_ROUTE_TOKEN, placeholder)
                self.global_tokens[FIXED_ROUTE_TOKEN] = placeholder

        self._server = await websockets.serve(
            self._handle_ios_client,
            host="",  # Bind to all interfaces for dual-stack IPv4/IPv6
            port=self.port,
        )
        log.info("iOS WebSocket listening on ws://[::1]:%d and ws://127.0.0.1:%d", self.port, self.port)
        await self._server.wait_closed()

    async def run_forever(self):
        assert self._server is not None
        await self._server.wait_closed()

    async def stop(self):
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _handle_ios_client(self, ws: WebSocketServerProtocol):
        """Handle a new iOS client WebSocket connection.

        Creates a new session, configures the socket for low latency,
        and processes incoming messages until disconnection.

        Args:
            ws: WebSocket connection from iOS client.
        """
        sess_id = str(uuid.uuid4())
        s       = Session(session_id=sess_id, ios_ws=ws)
        self.sessions[sess_id] = s

        # Configure socket for minimal latency
        try:
            ws.transport.get_extra_info("socket").setsockopt(
                socket.IPPROTO_TCP, socket.TCP_NODELAY, 1
            )
        except Exception:
            pass

        # Send initial connection confirmation with broker session ID
        # The iOS client will provide its chat_id in the "start" message
        await self._send(ws, {"event": "system", "type": "connected", "session_id": sess_id})
        log.info("iOS connected - %s", sess_id)

        await self._send(ws, {"event": "system", "type": "request_routes", "session_id": sess_id})

        try:
            async for raw in ws:
                msg = json.loads(raw)
                log.debug(f"Received from iOS: {msg.get('type')} - {msg}")
                await self._dispatch_ios_msg(s, msg)
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            await self._shutdown_session(s)
            log.info("iOS disconnected - %s", sess_id)

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

    async def _register_routes(self, s: Session, routes: list[dict[str, Any]]):
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
                register_route(FIXED_ROUTE_TOKEN, cfg)  # overwrite proxy mapping

        await self._send(s.ios_ws, {
            "event": "system",
            "type": "routes_registered",
            "tokens": added,
            "active_token": self.active_route_token,
            "stable_token": FIXED_ROUTE_TOKEN,
            "session_id": s.session_id
        })



    async def _dispatch_ios_msg(self, s: Session, msg: Dict[str, Any]):
        """Route incoming iOS messages to appropriate handlers.

        Processes different message types including ping, start, send,
        interrupt, stop, and permission responses.

        Args:
            s: Session for this iOS client.
            msg: Parsed JSON message from iOS.
        """
        try:
            match msg.get("type"):
                case "ping":
                    # Health check - respond with broker status
                    await self._send(s.ios_ws, {
                        "event": "pong",
                        "broker": "kisuke-broker2",
                        "version": "3.0",
                        "session_id": s.session_id,
                        "active_sessions": len(self.sessions)
                    })
                case "routes":
                    await self._register_routes(s, msg.get("payload", []))
                case "configure_upstream":
                    await self._register_routes(s, [msg.get("payload", {})])
                case "set_active_route":
                    tok = (msg.get("payload") or {}).get("token")
                    cfg = self.route_catalog.get(tok)
                    if cfg:
                        self.active_route_token = tok
                        # HOT SWITCH: overwrite the proxy's config behind the SINGLE fixed token
                        register_route(FIXED_ROUTE_TOKEN, cfg)

                        # Notify all tabs (purely informational; no restart needed)
                        for sess in self.sessions.values():
                            await self._send(sess.ios_ws, {
                                "event": "system",
                                "type": "active_route_changed",
                                "active_token": tok,
                                "stable_token": FIXED_ROUTE_TOKEN,
                                "session_id": sess.session_id
                            })
                    else:
                        await self._send(s.ios_ws, {
                            "event": "system",
                            "type": "error",
                            "error": "unknown_route_token",
                            "message": f"Token {tok} not registered",
                            "session_id": s.session_id
                        })

                case "start":
                    payload = msg.get("payload", {})
                    # Store iOS chat tab ID for message routing
                    if "tab_id" in payload:
                        s.chat_id = payload["tab_id"]
                        log.debug("Received chat_id from iOS: %s", s.chat_id)
                    await self._start_claude(s, payload)
                case "send":
                    await self._relay_user_message(s, msg.get("content", ""))
                case "interrupt":
                    if s.claude:
                        await s.claude.interrupt()
                case "stop":
                    await self._shutdown_session(s)
                case "permission_response":
                    await self._handle_permission_response(s, msg.get("payload", {}))
                case _:
                    log.warning("Unknown iOS message: %s", msg)
        except Exception as e:
            log.error("Error dispatching iOS message: %s", e, exc_info=True)
            # Send error back to iOS
            await self._send(s.ios_ws, {
                "event": "system",
                "type": "error",
                "error": "dispatch_error",
                "message": f"Error handling message: {str(e)}",
                "session_id": s.session_id
            })

    async def _start_claude(self, s: Session, cfg: Dict[str, Any]):
        """Initialize Claude SDK client for a session.

        Creates and connects a Claude SDK client with the specified
        configuration. Handles both new sessions and resumption.

        Args:
            s: Session to start Claude for.
            cfg: Configuration dictionary with options like permission_mode,
                workdir, system_prompt, etc.
        """
        if s.claude:  # If already connected, relay as next message
            await self._relay_user_message(s, cfg.get("prompt", ""))
            return

        try:
            log.info("Starting Claude client for session %s", s.session_id)
            log.info("   Config: %r", cfg)

            # Handle session resumption after plan mode or reconnection
            if cfg.get("resume"):
                log.info("RESUMING session %s with mode %s", cfg.get("resume"), cfg.get("permission_mode"))

            permission_mode = cfg.get("permission_mode", "bypassPermissions")
            log.debug("Starting with permission_mode: %s", permission_mode)

            # Initialize plan mode tracking
            if permission_mode == "plan":
                s.in_plan_mode = True
                # Store the original mode (from the UI) if not already stored
                if not s.original_permission_mode:
                    # Store the mode to return to after plan completion
                    s.original_permission_mode = cfg.get("original_permission_mode") or "default"
                    log.info("Entering plan mode, will return to: %s", s.original_permission_mode)

                    # Emit session state change event
                    await self._send(s.ios_ws, {
                        "event": "session_state_change",
                        "type": "permission_mode_change",
                        "from": s.original_permission_mode,
                        "to": "plan",
                        "reason": "user_initiated",
                        "session_id": s.session_id
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

            options = ClaudeCodeOptions(
                permission_mode = permission_mode,
                allowed_tools   = cfg.get("allowed_tools", []),
                cwd             = workdir,
                system_prompt   = cfg.get("system_prompt"),
                permission_prompt_tool_name = "stdio",   # Enable control channel for permissions
                resume = cfg.get("resume"),  # Support session resumption
            )

            async def _perm_handler(tool, inp, req_id):
                return await self._permission_flow(s, tool, inp, req_id)

            log.debug("Creating PermAwareClient with options: %r", options)
            s.claude = PermAwareClient(options=options, permission_handler=_perm_handler, session=s)

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
                    await s.claude.connect()
                finally:
                    # restore env
                    for k, v in prev.items():
                        if v is None: os.environ.pop(k, None)
                        else: os.environ[k] = v

            log.debug("Claude SDK connected successfully")

            # Emit session state event for subprocess connection
            await self._send(s.ios_ws, {
                "event": "session_state_change",
                "type": "subprocess_connected",
                "session_id": s.session_id,
                "permission_mode": permission_mode,
                "workdir": workdir,
                "tools_available": options.allowed_tools if options.allowed_tools else "all"
            })

            log.debug("Creating listener task...")
            s.listener_task = asyncio.create_task(self._claude_listener(s))

            log.info("Claude subprocess ready - %s", s.session_id)

            # Send init success event to iOS
            await self._send(s.ios_ws, {
                "event": "system",
                "type": "claude_ready",
                "session_id": s.session_id
            })

            initial_prompt = (cfg.get("prompt") or "").strip()
            if initial_prompt:
                log.info("Sending initial prompt (%d chars) for session %s",
                         len(initial_prompt), s.session_id)
                await self._relay_user_message(s, initial_prompt)
            else:
                log.debug("No initial prompt provided in start payload.")

        except Exception as e:
            log.error("Failed to start Claude client: %s", e, exc_info=True)

            # Clean up partial initialization
            if s.claude:
                try:
                    await s.claude.disconnect()
                except:
                    pass
                s.claude = None

            # Send error event to iOS instead of dropping connection
            await self._send(s.ios_ws, {
                "event": "system",
                "type": "error",
                "error": "claude_init_failed",
                "message": f"Failed to initialize Claude: {str(e)}",
                "session_id": s.session_id
            })

    # ------------------------------------------------------------ user → Claude
    async def _relay_user_message(self, s: Session, text: str):
        if not s.claude:
            return

        async def prompt_stream() -> AsyncIterator[Dict[str, Any]]:
            yield {
                "type":  "user",
                "message": {"role": "user", "content": text},
                "parent_tool_use_id": None,
                "session_id": s.session_id,
            }

        await s.claude.query(prompt_stream(), session_id=s.session_id)

    # ------------------------------------------------------- Claude → broker loop
    async def _claude_listener(self, s: Session):
        assert s.claude is not None

        async for msg in s.claude.receive_messages():
            # Log the message type for debugging
            log.debug("📨 _claude_listener received message type: %s", type(msg).__name__)

            # Normal message handling
            await self._emit_to_ios(s, msg)

    async def _emit_to_ios(self, s: Session, m):
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
                    s.pending_tools[b.id] = {
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
                **s.ctx()
            }

            # Add message metadata if available
            message_info = {}
            for attr in ['id', 'type', 'role', 'model', 'stop_reason', 'stop_sequence', 'usage']:
                if hasattr(m, attr):
                    message_info[attr] = getattr(m, attr)

            if message_info:
                message_data["message"] = message_info

            await self._send(s.ios_ws, message_data)

        elif isinstance(m, UserMessage):
            # Handle tool results that come back as UserMessages
            log.debug("🎯 _emit_to_ios handling UserMessage with %d content items", len(m.content))
            tool_results: list[Dict[str, Any]] = []

            for item in m.content:
                if isinstance(item, ToolResultBlock):
                    log.debug("📊 Tool result received: tool_use_id=%s, is_error=%s",
                            item.tool_use_id, item.is_error)

                    # Get tool info from pending tools
                    tool_info = s.pending_tools.get(item.tool_use_id, {})
                    tool_name = tool_info.get("name", "unknown")

                    # Send individual tool result event
                    tool_result_event = {
                        "event": "tool_result",
                        "tool_use_id": item.tool_use_id,
                        "tool_name": tool_name,
                        "content": item.content,
                        "is_error": item.is_error,
                        **s.ctx()
                    }

                    # Add error details if available
                    if item.is_error:
                        tool_result_event["error_type"] = "tool_execution_error"
                        log.info("❌ Tool error for %s: %s", tool_name, item.content)
                    else:
                        log.info("✅ Tool success for %s", tool_name)

                    await self._send(s.ios_ws, tool_result_event)

                    # Also emit tool completion event
                    await self._send(s.ios_ws, {
                        "event": "tool_execution",
                        "type": "complete",
                        "tool_use_id": item.tool_use_id,
                        "tool_name": tool_name,
                        "success": not item.is_error,
                        "error": item.content if item.is_error else None,
                        **s.ctx()
                    })

                    # Clean up pending tool
                    s.pending_tools.pop(item.tool_use_id, None)

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
                await self._send(s.ios_ws, {
                    "event": "stream",
                    "data": tool_results,
                    "message_type": "user",
                    **s.ctx()
                })

        elif isinstance(m, ResultMessage):
            s.used_tokens += (m.usage or {}).get("input_tokens", 0)

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
                "usage": m.usage,
                **s.ctx(),
            }
            await self._send(s.ios_ws, result_data)

        elif isinstance(m, SystemMessage):
            # Include all system message fields
            system_data = {
                "event": "system",
                "subtype": m.subtype,
                "chat_id": s.chat_id,  # Add chat_id for iOS routing
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

                # Capture Claude's session ID from the raw message we stored
                if hasattr(s.claude, '_init_session_id'):
                    s.claude_session_id = s.claude._init_session_id
                    log.info("Captured Claude session ID from _init_session_id: %s", s.claude_session_id)
                elif hasattr(m, 'id'):
                    s.claude_session_id = m.id
                    log.info("Captured Claude session ID from message.id: %s", s.claude_session_id)
                elif hasattr(m, 'session_id'):
                    s.claude_session_id = m.session_id
                    log.info("Captured Claude session ID from message.session_id: %s", s.claude_session_id)
                else:
                    log.warning("⚠️ Init message missing session ID field")
                    log.debug("🔍 Init message attributes: %s", [attr for attr in dir(m) if not attr.startswith('_')])
            else:
                # For non-init messages, copy specific fields
                for attr in ['cwd', 'session_id', 'tools', 'mcp_servers', 'model', 'permissionMode', 'apiKeySource']:
                    if hasattr(m, attr):
                        system_data[attr] = getattr(m, attr)

            await self._send(s.ios_ws, system_data)

    async def _permission_flow(
        self, s: Session, tool_name: str, tool_input: Dict[str, Any], req_id: str
    ) -> Dict[str, Any]:
        """Handle tool permission request flow.

        Checks cached permissions first, then forwards to iOS client if needed.
        Tracks permission state and emits lifecycle events.

        Args:
            s: Session requesting permission.
            tool_name: Name of the tool requesting permission.
            tool_input: Input parameters for the tool.
            req_id: Unique request identifier.

        Returns:
            Permission response dictionary with behavior and optional message.
        """
        log.debug("_permission_flow called for tool=%s, req_id=%s", tool_name, req_id)
        log.debug("   Session allowed_tools: %s", s.allowed_tools)
        log.debug("   Session denied_tools: %s", s.denied_tools)

        # immediate answers from cached choices
        if tool_name in s.allowed_tools:
            log.debug("   Tool '%s' is pre-allowed, returning immediate allow", tool_name)
            return {"behavior": "allow"}
        if tool_name in s.denied_tools:
            log.debug("   Tool '%s' is pre-denied, returning immediate deny", tool_name)
            return {"behavior": "deny", "message": f"{tool_name} was previously denied"}

        fut: asyncio.Future = asyncio.Future()
        s.pending_permissions[req_id] = fut

        await self._send(
            s.ios_ws,
            {
                "event": "permission_request",
                "chat_id": s.chat_id,  # Use chat_id for iOS routing
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
                await self._send(s.ios_ws, {
                    "event": "tool_execution",
                    "type": "start",
                    "tool_name": tool_name,
                    "request_id": req_id,
                    "session_id": s.session_id,
                    "chat_id": s.chat_id
                })
            elif resp.get("behavior") == "deny":
                await self._send(s.ios_ws, {
                    "event": "tool_execution",
                    "type": "denied",
                    "tool_name": tool_name,
                    "request_id": req_id,
                    "reason": resp.get("message", f"User denied {tool_name}"),
                    "session_id": s.session_id,
                    "chat_id": s.chat_id
                })

            return resp
        finally:
            s.pending_permissions.pop(req_id, None)

    async def _handle_permission_response(self, s: Session, payload: Dict[str, Any]):
        """Process permission response from iOS client.

        Handles the iOS client's response to a permission request, including
        special handling for ExitPlanMode and permanent permission settings.

        Args:
            s: Session that received the response.
            payload: Response data including behavior, tool_name, and flags.
        """
        req_id   = payload.get("request_id")
        behavior = payload.get("behavior")            # "allow" | "deny"
        tool     = payload.get("tool_name")
        always   = payload.get("perm_always", False)

        fut = s.pending_permissions.get(req_id)
        if fut is None:
            log.warning("Unknown permission request id %s", req_id)
            return

        # Special handling for ExitPlanMode approval
        if tool == "ExitPlanMode":
            if behavior == "allow":
                log.info("ExitPlanMode approved for session %s", s.session_id)

                # Determine target mode based on 'always' flag
                target_mode = "acceptEdits" if always else "default"
                log.info("Plan approved! Will exit plan mode and resume with %s permissions", target_mode)

                # Get current session info
                session_id = s.claude_session_id
                cwd = s.claude.options.cwd if s.claude and s.claude.options else None

                if not session_id:
                    log.error("Cannot exit plan mode: no Claude session ID available")
                    fut.set_result({"behavior": "deny", "message": "No session ID available for plan mode exit"})
                    return

                log.info("Immediately transitioning from plan mode to %s", target_mode)
                log.info("   Claude session ID: %s", session_id)
                log.info("   Working directory: %s", cwd)

                # Create the response for Claude
                resp = {
                    "behavior": behavior,
                    "perm_always": always,
                    "message": payload.get("message"),
                }

                # First, resolve the permission future so Claude gets the response
                fut.set_result(resp)
                s.pending_permissions.pop(req_id, None)

                # Reset plan mode state
                s.in_plan_mode = False

                # Shutdown current Claude subprocess
                await self._shutdown_session(s, keep_session=True)

                # Small delay to ensure clean shutdown
                await asyncio.sleep(0.5)

                # Restart with new permission mode
                resume_config = {
                    "permission_mode": target_mode,
                    "resume": session_id,
                    "workdir": cwd,
                }

                log.info("Restarting Claude with permission_mode=%s", target_mode)
                await self._start_claude(s, resume_config)

                # Send mode_changed event to iOS
                await self._send(s.ios_ws, {
                    "event": "system",
                    "type": "mode_changed",
                    "from": "plan",
                    "to": target_mode,
                    "session_id": s.session_id,
                    "chat_id": s.chat_id
                })

                return  # Don't process the response again below
            else:
                # Plan rejected - interrupt Claude execution
                log.info("Plan denied! Interrupting...")
                if s.claude:
                    asyncio.create_task(s.claude.interrupt())

        # remember long-term choices
        if always:
            (s.allowed_tools if behavior == "allow" else s.denied_tools).add(tool)

        # notify UI for optimistic updates
        await self._send(
            s.ios_ws,
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

    async def _shutdown_session(self, s: Session, keep_session: bool = False):
        """Clean up session resources.

        Cancels listener task, disconnects Claude client, and optionally
        removes session from active sessions.

        Args:
            s: Session to shut down.
            keep_session: If True, preserve session for resumption.
        """
        # Emit session state change event before shutdown
        if s.claude:
            await self._send(s.ios_ws, {
                "event": "session_state_change",
                "type": "subprocess_disconnecting",
                "session_id": s.session_id,
                "reason": "resuming" if keep_session else "shutdown",
                "chat_id": s.chat_id
            })

        if s.listener_task and not s.listener_task.done():
            s.listener_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await s.listener_task
        if s.claude:
            await s.claude.disconnect()
            s.claude = None
        s.listener_task = None

        # Only remove from sessions if not keeping for resumption
        if not keep_session:
            self.sessions.pop(s.session_id, None)




async def _main():
    """Main entry point for the broker.

    Sets up signal handlers and starts the WebSocket server.
    """
    broker = KisukeBrokerSDK()
    stop = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    # start both WS server and embedded proxy
    server_task = asyncio.create_task(broker.start())

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
